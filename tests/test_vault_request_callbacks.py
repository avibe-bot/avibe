"""P4: vault-request auto-resume callbacks.

A request transition to a terminal state arms ``callback_status='pending'``; the daemon sweep
turns each armed row into exactly one callback turn to the requesting session via the shared
``enqueue_session_callback`` entry (``source_kind='callback'``), then marks it sent/skipped.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from storage import vault_service as vs
from storage.db import create_sqlite_engine
from storage.models import metadata, vault_requests
from storage.vault_crypto import Sealed


@pytest.fixture
def vault(tmp_path):
    vs.GRANT_RUNTIME_CACHE.clear()
    engine = create_sqlite_engine(tmp_path / "vault_cb.sqlite")
    metadata.create_all(engine)
    return engine


def _sealed(suffix: str = "1") -> Sealed:
    return Sealed(ciphertext=f"ct-{suffix}", nonce=f"n-{suffix}", wrap_meta=f"wm-{suffix}")


def _signing_public_meta() -> dict:
    return {
        "signing_public_key": {
            "curve": "secp256k1",
            "public_key": "02" + ("01" * 32),
        }
    }


def _valid_recoverable_signature() -> dict:
    return {"signature": "ab" * 64, "recovery_id": 0}


def _create_protected_keypair(conn, name: str = "ETH_KEY") -> None:
    vs.create_secret(
        conn,
        name=name,
        sealed=_sealed("sign"),
        protection="protected",
        kind="keypair",
        signer_kind="local",
        public_meta=_signing_public_meta(),
    )


def _callback_status(conn, request_id: str):
    return conn.execute(
        select(vault_requests.c.callback_status).where(vault_requests.c.id == request_id)
    ).scalar_one()


def _row(conn, request_id: str) -> dict:
    return dict(conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().one())


def _grant_from_request(conn, request: dict) -> dict:
    option = request["card"]["grant_options"][0]
    return vs.create_grant(
        conn,
        member_names=option["member_snapshot"],
        source_selector=option["source_selector"],
        purpose=option["purpose"],
        request_id=request["id"],
        cache_ready=True,
    )


def _grant_from_request_with_cache_ready(conn, request: dict, *, cache_ready: bool) -> dict:
    option = request["card"]["grant_options"][0]
    return vs.create_grant(
        conn,
        member_names=option["member_snapshot"],
        source_selector=option["source_selector"],
        purpose=option["purpose"],
        request_id=request["id"],
        cache_ready=cache_ready,
    )


# --- terminal transitions arm the callback --------------------------------------------------


def test_access_approval_arms_callback(vault):
    with vault.begin() as conn:
        vs.create_secret(conn, name="A_KEY", sealed=_sealed(), protection="protected")
        req = vs.create_access_request(conn, "A_KEY", requester={"session_id": "ses_a"})
        _grant_from_request(conn, req)
        assert _callback_status(conn, req["id"]) == "pending"
        plan = vs.resolve_request_callback(_row(conn, req["id"]))
        assert plan is not None
        assert plan.session_id == "ses_a"
        assert "approved" in plan.message.lower()


def test_deny_arms_callback(vault):
    with vault.begin() as conn:
        vs.create_secret(conn, name="D_KEY", sealed=_sealed(), protection="protected")
        req = vs.create_access_request(conn, "D_KEY", requester={"session_id": "ses_d"})
        vs.deny_request(conn, req["id"])
        assert _callback_status(conn, req["id"]) == "pending"
        plan = vs.resolve_request_callback(_row(conn, req["id"]))
        assert plan is not None and "declined" in plan.message.lower()


def test_provision_fulfill_arms_callback(vault):
    with vault.begin() as conn:
        req = vs.create_provision_request(conn, "NEW_KEY", requester={"session_id": "ses_p"})
        assert req["status"] == "pending"
        assert vs.fulfill_pending_provision_requests_for_secret(conn, "NEW_KEY") == 1
        assert _callback_status(conn, req["id"]) == "pending"
        plan = vs.resolve_request_callback(_row(conn, req["id"]))
        assert plan is not None and "provided" in plan.message.lower()


def test_expiry_arms_callback(vault):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with vault.begin() as conn:
        vs.create_secret(conn, name="E_KEY", sealed=_sealed(), protection="protected")
        req = vs.create_access_request(conn, "E_KEY", requester={"session_id": "ses_e"})
        conn.execute(vault_requests.update().where(vault_requests.c.id == req["id"]).values(expires_at=past))
        expired = vs._load_request_row(conn, req["id"])  # lazy expiry on read
        assert expired["status"] == "expired"
        assert _callback_status(conn, req["id"]) == "pending"
        plan = vs.resolve_request_callback(_row(conn, req["id"]))
        assert plan is not None and "expired" in plan.message.lower()


def test_protected_browser_sign_approval_arms_callback(vault):
    with vault.begin() as conn:
        _create_protected_keypair(conn)
        req = vs.create_sign_request(
            conn,
            "ETH_KEY",
            digest="00" * 32,
            scheme="ecdsa-secp256k1-recoverable",
            requester={"session_id": "ses_sign", "source": "agent-cli"},
            delivery={"session_id": "ses_sign", "command": "sign:ecdsa-secp256k1-recoverable"},
        )

        vs.complete_sign_request(
            conn,
            req["id"],
            name="ETH_KEY",
            digest="00" * 32,
            scheme="ecdsa-secp256k1-recoverable",
            signature=_valid_recoverable_signature(),
            browser_signed=True,
        )

        row = _row(conn, req["id"])
        assert row["callback_status"] == "pending"
        plan = vs.resolve_request_callback(row)
        assert plan is not None
        assert plan.session_id == "ses_sign"
        assert plan.message.strip()
        assert "vault await" in plan.message
        assert vs.request_callback_ready(conn, row) is True


def test_failed_sign_request_arms_callback(vault):
    with vault.begin() as conn:
        _create_protected_keypair(conn)
        req = vs.create_sign_request(
            conn,
            "ETH_KEY",
            digest="00" * 32,
            scheme="ecdsa-secp256k1-recoverable",
            requester={"session_id": "ses_sign", "source": "agent-cli"},
            delivery={"session_id": "ses_sign"},
        )
        vs.claim_sign_request(conn, req["id"], name="ETH_KEY", digest="00" * 32, scheme="ecdsa-secp256k1-recoverable")

        vs.fail_sign_request(conn, req["id"], reason="browser_signature_failed")

        row = _row(conn, req["id"])
        assert row["callback_status"] == "pending"
        plan = vs.resolve_request_callback(row)
        assert plan is not None
        assert plan.session_id == "ses_sign"
        assert plan.message.strip()
        assert "signing error" in plan.message.lower()
        assert vs.request_callback_ready(conn, row) is True


# --- skip conditions ------------------------------------------------------------------------


def test_optout_arms_but_resolves_to_skip(vault):
    with vault.begin() as conn:
        vs.create_secret(conn, name="O_KEY", sealed=_sealed(), protection="protected")
        req = vs.create_access_request(conn, "O_KEY", requester={"session_id": "ses_o", "callback_disabled": True})
        _grant_from_request(conn, req)
        # Transition still arms the row (dumb marker); the sweep decides to skip.
        assert _callback_status(conn, req["id"]) == "pending"
        assert vs.resolve_request_callback(_row(conn, req["id"])) is None


def test_no_session_resolves_to_skip(vault):
    with vault.begin() as conn:
        vs.create_secret(conn, name="N_KEY", sealed=_sealed(), protection="protected")
        req = vs.create_access_request(conn, "N_KEY")  # no requester → no session
        _grant_from_request(conn, req)
        assert vs.resolve_request_callback(_row(conn, req["id"])) is None


# --- sweep bookkeeping + exactly-once -------------------------------------------------------


def test_list_and_mark_are_exclusive(vault):
    with vault.begin() as conn:
        req = vs.create_provision_request(conn, "L_KEY", requester={"session_id": "ses_l"})
        vs.fulfill_pending_provision_requests_for_secret(conn, "L_KEY")
        assert [r["id"] for r in vs.list_pending_request_callbacks(conn)] == [req["id"]]
        vs.mark_request_callback(conn, req["id"], status="sent")
        assert vs.list_pending_request_callbacks(conn) == []
        assert _callback_status(conn, req["id"]) == "sent"


def test_list_pending_request_callbacks_returns_all_pending_rows(vault):
    with vault.begin() as conn:
        for index in range(55):
            req = vs.create_provision_request(conn, f"PAGE_KEY_{index}", requester={"session_id": f"ses_{index}"})
            vs.fulfill_pending_provision_requests_for_secret(conn, f"PAGE_KEY_{index}")
            assert _callback_status(conn, req["id"]) == "pending"

        assert len(vs.list_pending_request_callbacks(conn, limit=50)) == 50
        assert len(vs.list_pending_request_callbacks(conn)) == 55


def test_reresolve_does_not_rearm(vault):
    with vault.begin() as conn:
        vs.create_secret(conn, name="R_KEY", sealed=_sealed(), protection="protected")
        req = vs.create_access_request(conn, "R_KEY", requester={"session_id": "ses_r"})
        vs.deny_request(conn, req["id"])
        vs.mark_request_callback(conn, req["id"], status="sent")
        # The atomic WHERE status='pending' claim rejects a re-resolve, so the callback can't rearm.
        with pytest.raises(vs.InvalidRequestError):
            vs.deny_request(conn, req["id"])
        assert _callback_status(conn, req["id"]) == "sent"


# --- message coverage (pure) ----------------------------------------------------------------


@pytest.mark.parametrize(
    "request_type,status,needle",
    [
        ("provision", "fulfilled", "provided"),
        ("access", "approved", "approved"),
        ("sign", "approved", "approved"),
        ("access", "denied", "declined"),
        ("sign", "failed", "signing error"),
        ("provision", "expired", "expired"),
        ("access", "expired", "expired"),
    ],
)
def test_message_and_session_by_type_status(request_type, status, needle):
    row = {
        "id": "vrq_msg",
        "request_type": request_type,
        "status": status,
        "secret_name": "K",
        "requester": json.dumps({"session_id": "ses_m"}),
        "delivery": None,
    }
    plan = vs.resolve_request_callback(row)
    assert plan is not None
    assert plan.session_id == "ses_m"
    assert needle in plan.message.lower()


def test_only_no_callback_disables_auto_resume_at_creation():
    # Only --no-callback pre-disables. --wait must NOT: a finite wait can time out with the
    # request still pending, and the agent must still be auto-resumed when it later resolves.
    # (A wait that observes fulfillment suppresses the redundant callback at that point instead.)
    from types import SimpleNamespace

    from vibe import cli

    assert cli._vault_callback_disabled(SimpleNamespace(no_callback=True, wait=None)) is True
    assert cli._vault_callback_disabled(SimpleNamespace(no_callback=False, wait=5.0)) is False
    assert cli._vault_callback_disabled(SimpleNamespace(no_callback=False, wait=None)) is False


def test_expire_overdue_requests_arms_callback(vault):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with vault.begin() as conn:
        req = vs.create_provision_request(conn, "T_KEY", requester={"session_id": "ses_t"})
        conn.execute(vault_requests.update().where(vault_requests.c.id == req["id"]).values(expires_at=past))
        # Untouched: still pending, no callback armed yet (expiry is lazy).
        assert _callback_status(conn, req["id"]) is None
        vs.expire_overdue_requests(conn)
        assert _row(conn, req["id"])["status"] == "expired"
        assert _callback_status(conn, req["id"]) == "pending"


def test_request_callback_ready_defers_approved_access_until_grant_ready(vault):
    with vault.begin() as conn:
        vs.create_secret(conn, name="GR_KEY", sealed=_sealed(), protection="protected")
        req = vs.create_access_request(conn, "GR_KEY", requester={"session_id": "ses_gr"})
        grant = _grant_from_request_with_cache_ready(conn, req, cache_ready=False)
        row = _row(conn, req["id"])

        assert vs.request_callback_ready(conn, row) is False  # protected relay in flight → defer

        vs.mark_grant_agent_ready(conn, grant["id"])
        row = _row(conn, req["id"])
        assert vs.request_callback_ready(conn, row) is True  # grant ready → deliver

        req_without_grant = vs.create_access_request(conn, "GR_KEY", requester={"session_id": "ses_missing"})
        conn.execute(vault_requests.update().where(vault_requests.c.id == req_without_grant["id"]).values(status="approved"))
        assert vs.request_callback_ready(conn, _row(conn, req_without_grant["id"])) is True

    # Non-access or non-approved terminal states are always deliverable (no grant lookup).
    assert vs.request_callback_ready(None, {"request_type": "provision", "status": "fulfilled", "id": "x"}) is True
    assert vs.request_callback_ready(None, {"request_type": "access", "status": "denied", "id": "y"}) is True


def test_request_callback_ready_defers_sibling_access_until_covering_grant_ready(vault):
    with vault.begin() as conn:
        vs.create_secret(conn, name="GR_A", sealed=_sealed("a"), protection="protected", tags=["deploy"])
        vs.create_secret(conn, name="GR_B", sealed=_sealed("b"), protection="protected", tags=["deploy"])
        primary = vs.create_access_request(
            conn,
            source_selector={"tags": ["deploy"]},
            requester={"session_id": "ses_gr"},
            delivery={"session_id": "ses_gr"},
        )
        sibling = vs.create_access_request(
            conn,
            "GR_A",
            requester={"session_id": "ses_gr"},
            delivery={"session_id": "ses_gr"},
        )

        grant = _grant_from_request_with_cache_ready(conn, primary, cache_ready=False)
        sibling_row = _row(conn, sibling["id"])

        assert sibling_row["status"] == "approved"
        assert _callback_status(conn, sibling["id"]) == "pending"
        assert vs.get_grant_created_by_request(conn, sibling["id"]) is None
        assert vs.request_callback_ready(conn, sibling_row) is False

        vs.mark_grant_agent_ready(conn, grant["id"])
        assert vs.request_callback_ready(conn, _row(conn, sibling["id"])) is True


def test_access_request_restore_clears_callback_status_for_retry(vault):
    with vault.begin() as conn:
        vs.create_secret(conn, name="RB_A", sealed=_sealed("a"), protection="protected", tags=["deploy"])
        vs.create_secret(conn, name="RB_B", sealed=_sealed("b"), protection="protected", tags=["deploy"])
        primary = vs.create_access_request(
            conn,
            source_selector={"tags": ["deploy"]},
            requester={"session_id": "ses_rb"},
            delivery={"session_id": "ses_rb"},
        )
        sibling = vs.create_access_request(
            conn,
            "RB_A",
            requester={"session_id": "ses_rb"},
            delivery={"session_id": "ses_rb"},
        )
        grant = _grant_from_request_with_cache_ready(conn, primary, cache_ready=False)

        restored = vs.restore_access_request_after_failed_grant(
            conn,
            request_id=primary["id"],
            member_names=grant["member_snapshot"],
            session_id=grant["session_id"],
        )

        assert restored == 2
        assert _row(conn, primary["id"])["status"] == "pending"
        assert _row(conn, sibling["id"])["status"] == "pending"
        assert _callback_status(conn, primary["id"]) is None
        assert _callback_status(conn, sibling["id"]) is None


def test_access_grant_create_rollback_clears_callback_status(vault):
    with vault.begin() as conn:
        vs.create_secret(conn, name="RB_CONFLICT", sealed=_sealed(), protection="protected")
        first = vs.create_access_request(conn, "RB_CONFLICT", requester={"session_id": "ses_one"})
        existing = _grant_from_request(conn, first)
        second = vs.create_access_request(conn, "RB_CONFLICT", requester={"session_id": "ses_two"})

        second["card"]["grant_options"][0]["grant_id"] = existing["id"]
        delivery = json.loads(_row(conn, second["id"])["delivery"])
        delivery["card"]["grant_options"][0]["grant_id"] = existing["id"]
        conn.execute(vault_requests.update().where(vault_requests.c.id == second["id"]).values(delivery=json.dumps(delivery)))

        with pytest.raises(vs.InvalidGrantError):
            _grant_from_request(conn, second)

        row = _row(conn, second["id"])
        assert row["status"] == "pending"
        assert row["decided_at"] is None
        assert row["callback_status"] is None


def test_sign_approved_callback_points_to_the_signature():
    # The signature is the deliverable; the wake message must tell the agent how to get it.
    row = {
        "id": "vrq_sig",
        "request_type": "sign",
        "status": "approved",
        "secret_name": "KEYPAIR",
        "requester": json.dumps({"session_id": "ses_s"}),
        "delivery": None,
    }
    plan = vs.resolve_request_callback(row)
    assert plan is not None
    assert "vrq_sig" in plan.message
    assert "Retrieve the signature result with: vibe vault await vrq_sig" in plan.message
    assert "Do not rerun `vibe vault sign`" in plan.message


def test_callback_enabled_followup_does_not_suggest_await(monkeypatch):
    from types import SimpleNamespace

    from vibe import cli

    # Callback armed → must NOT suggest `vault await` (awaiting would double-resume).
    enabled = cli._vault_request_followup_message(
        SimpleNamespace(no_callback=False, wait=None, session_id="ses_x"), "vrq_x", resolved_verb="approves or denies it"
    )
    assert "await" not in enabled.lower()
    assert "--no-callback" in enabled  # points at the correct way to block synchronously
    # Opt-out path (no callback armed) DOES point at await.
    opted_out = cli._vault_request_followup_message(
        SimpleNamespace(no_callback=True, wait=None, session_id="ses_x"), "vrq_x", resolved_verb="approves or denies it"
    )
    assert "vault await" in opted_out

    monkeypatch.delenv("AVIBE_SESSION_ID", raising=False)
    no_session = cli._vault_request_followup_message(
        SimpleNamespace(no_callback=False, wait=None, session_id=None), "vrq_x", resolved_verb="approves or denies it"
    )
    assert "vault await" in no_session


def test_non_terminal_status_has_no_callback():
    row = {
        "id": "vrq_pending",
        "request_type": "access",
        "status": "pending",
        "secret_name": "K",
        "requester": json.dumps({"session_id": "ses_m"}),
        "delivery": None,
    }
    assert vs.resolve_request_callback(row) is None


# --- shared enqueue entry uses the same callback path Agent Run uses -------------------------


def test_enqueue_session_callback_uses_callback_source(monkeypatch):
    from core import scheduled_tasks as st

    class _Key:
        def to_key(self) -> str:
            return "plat::channel::c1"

    class _Target:
        session_key = _Key()
        agent_name = "agentx"
        agent_id = "aid"
        agent_backend = "claude"
        model = None
        reasoning_effort = None

    calls: dict = {}

    class _Store:
        def enqueue_agent_run(self, **kw):
            calls.update(kw)
            return type("R", (), {"id": "run_1"})()

    monkeypatch.setattr(st, "resolve_session_id_target", lambda sid: _Target())

    out = st.enqueue_session_callback(_Store(), session_id="ses_x", message="resume now", source_actor="vault:vrq_1")
    assert out is not None and out.id == "run_1"
    assert calls["source_kind"] == "callback"
    assert calls["session_policy"] == "existing"
    assert calls["session_id"] == "ses_x"
    assert calls["message"] == "resume now"
    assert calls["source_actor"] == "vault:vrq_1"

    # Nothing to send → no enqueue.
    assert st.enqueue_session_callback(_Store(), session_id="", message="x", source_actor="a") is None
    assert st.enqueue_session_callback(_Store(), session_id="ses", message="   ", source_actor="a") is None


def test_vault_callback_sweep_enqueues_protected_sign_callback(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    from core import scheduled_tasks as st

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    engine = create_sqlite_engine()
    metadata.create_all(engine)
    with engine.begin() as conn:
        _create_protected_keypair(conn)
        req = vs.create_sign_request(
            conn,
            "ETH_KEY",
            digest="00" * 32,
            scheme="ecdsa-secp256k1-recoverable",
            requester={"session_id": "ses_sign", "source": "agent-cli"},
            delivery={"session_id": "ses_sign"},
        )
        vs.complete_sign_request(
            conn,
            req["id"],
            name="ETH_KEY",
            digest="00" * 32,
            scheme="ecdsa-secp256k1-recoverable",
            signature=_valid_recoverable_signature(),
            browser_signed=True,
        )

    class _Key:
        def to_key(self) -> str:
            return "avibe::scope::ses_sign"

    target = SimpleNamespace(
        session_key=_Key(),
        agent_name="codex",
        agent_id="aid",
        agent_backend="codex",
        model=None,
        reasoning_effort=None,
    )
    monkeypatch.setattr(st, "resolve_session_id_target", lambda session_id: target)
    request_store = st.TaskExecutionStore(tmp_path / "task_requests")
    service = st.ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=st.ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    asyncio.run(service._drain_vault_callbacks())

    with engine.connect() as conn:
        assert _callback_status(conn, req["id"]) == "sent"
    [callback_run] = request_store.list_pending()
    assert callback_run.session_id == "ses_sign"
    assert callback_run.session_policy == "existing"
    assert callback_run.source_kind == "callback"
    assert callback_run.source_actor == f"vault:{req['id']}"
    assert callback_run.message
    assert "Retrieve the signature result with: vibe vault await" in callback_run.message
    assert "Do not rerun `vibe vault sign`" in callback_run.message
