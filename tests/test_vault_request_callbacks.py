"""P4: vault-request auto-resume callbacks.

A request transition to a terminal state arms ``callback_status='pending'``. The daemon sweep
starts one callback turn for asynchronous results, or mirrors a transcript-only Vault outcome when
a synchronous CLI waiter already returned the result, then marks the callback sent/skipped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from core.message_mirror import mirror_harness_inbound
from modules.im import MessageContext
from storage import vault_service as vs
from storage import messages_service
from storage.db import create_sqlite_engine
from storage.models import metadata, vault_requests, vault_secrets
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


@pytest.mark.parametrize("with_spec", [False, True])
def test_field_provision_callback_reports_current_value_free_fields(vault, with_spec):
    with vault.begin() as conn:
        req = vs.create_provision_request(
            conn, "LOGIN", requester={"session_id": "ses_p"},
            spec={"fields": ["username", "password", "totp"], "default_field": "password"} if with_spec else None,
        )
        vs.create_secret(
            conn, name="LOGIN", default_field="password", sealed=_sealed("do-not-show-default"),
            fields=[{"name": "username", "sealed": _sealed("do-not-show-extra")}],
        )
        vs.update_secret_metadata(conn, "LOGIN", default_field="credential")
        pending = next(row for row in vs.list_pending_request_callbacks(conn) if row["id"] == req["id"])
        plan = vs.resolve_request_callback(pending)
        assert plan is not None
        assert "credential" in plan.message and "username" in plan.message
        assert "LOGIN.username" in plan.message or "LOGIN.credential" in plan.message
        assert "--env" in plan.message
        assert "totp" not in plan.message and "password" not in plan.message
        assert "do-not-show" not in plan.message
        assert "ciphertext" not in plan.message
        # The callback decides on the current row and must keep the same current-field projection.
        claim = vs.claim_request_callback(conn, req["id"])
        assert claim.action == "deliver"
        assert claim.plan.message == plan.message


@pytest.mark.parametrize(
    ("default_field", "corrupt", "expected"),
    [("password", False, "LOGIN.password"), ("value", True, "corrupt")],
)
def test_async_field_provision_callback_attaches_single_custom_and_corrupt_shapes(
    vault, default_field, corrupt, expected,
):
    with vault.begin() as conn:
        req = vs.create_provision_request(conn, "LOGIN", requester={"session_id": "ses_p"})
        vs.create_secret(conn, name="LOGIN", default_field=default_field, sealed=_sealed("do-not-show"))
        if corrupt:
            conn.execute(
                vault_secrets.update()
                .where(vault_secrets.c.name == "LOGIN")
                .values(fields="{malformed")
            )
        pending = next(row for row in vs.list_pending_request_callbacks(conn) if row["id"] == req["id"])
        plan = vs.resolve_request_callback(pending)
        assert plan is not None
        assert expected in plan.message
        assert "Current fields:" in plan.message
        assert "do-not-show" not in plan.message


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
    # (A provision wait suppresses its callback; access waiters retain a transcript-only outcome.)
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


def test_request_callback_ready_defers_while_cli_waiter_is_active(vault):
    future = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    with vault.begin() as conn:
        vs.create_secret(conn, name="WAIT_GR_KEY", sealed=_sealed(), protection="protected")
        req = vs.create_access_request(conn, "WAIT_GR_KEY", requester={"session_id": "ses_wait"})
        vs.arm_request_waiter(conn, req["id"], waiter_id="vw_test", deadline_at=future)
        _grant_from_request(conn, req)

        row = _row(conn, req["id"])
        assert row["callback_status"] == "pending"
        assert vs.request_callback_ready(conn, row) is False

        vs.timeout_request_waiter(conn, req["id"], waiter_id="vw_test")
        row = _row(conn, req["id"])
        assert vs.request_callback_ready(conn, row) is True


def test_complete_request_waiter_keeps_visible_outcome_pending(vault):
    future = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    with vault.begin() as conn:
        vs.create_secret(conn, name="DONE_GR_KEY", sealed=_sealed(), protection="protected")
        req = vs.create_access_request(conn, "DONE_GR_KEY", requester={"session_id": "ses_done"})
        vs.arm_request_waiter(conn, req["id"], waiter_id="vw_done", deadline_at=future)
        _grant_from_request(conn, req)

        vs.complete_request_waiter(conn, req["id"], waiter_id="vw_done")

        row = _row(conn, req["id"])
        assert row["callback_status"] == "pending"
        assert vs.request_callback_consumed_by_waiter(row) is True
        assert [pending["id"] for pending in vs.list_pending_request_callbacks(conn)] == [req["id"]]


def test_covering_grant_owner_waiter_suppresses_sibling_callbacks(vault):
    future = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    with vault.begin() as conn:
        vs.create_secret(conn, name="WAIT_A", sealed=_sealed("a"), protection="protected", tags=["deploy"])
        vs.create_secret(conn, name="WAIT_B", sealed=_sealed("b"), protection="protected", tags=["deploy"])
        primary = vs.create_access_request(
            conn,
            source_selector={"tags": ["deploy"]},
            requester={"session_id": "ses_wait"},
            delivery={"session_id": "ses_wait"},
        )
        sibling = vs.create_access_request(
            conn,
            "WAIT_A",
            requester={"session_id": "ses_wait"},
            delivery={"session_id": "ses_wait"},
        )
        vs.arm_request_waiter(conn, primary["id"], waiter_id="vw_primary", deadline_at=future)
        _grant_from_request(conn, primary)

        sibling_row = _row(conn, sibling["id"])
        assert sibling_row["status"] == "approved"
        assert sibling_row["callback_status"] == "pending"
        assert vs.request_callback_ready(conn, sibling_row) is False

        vs.complete_request_waiter(conn, primary["id"], waiter_id="vw_primary")

        assert _callback_status(conn, primary["id"]) == "pending"
        assert _callback_status(conn, sibling["id"]) == "skipped"
        assert [pending["id"] for pending in vs.list_pending_request_callbacks(conn)] == [primary["id"]]


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

    out = st.enqueue_session_callback(
        _Store(),
        session_id="ses_x",
        message="resume now",
        source_actor="vault:vrq_1",
        metadata={"vault_request_status": "denied"},
    )
    assert out is not None and out.id == "run_1"
    assert calls["source_kind"] == "callback"
    assert calls["session_policy"] == "existing"
    assert calls["session_id"] == "ses_x"
    assert calls["message"] == "resume now"
    assert calls["source_actor"] == "vault:vrq_1"
    assert calls["metadata"] == {"vault_request_status": "denied"}

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


@pytest.mark.parametrize("terminal_status", ["approved", "denied"])
def test_legacy_callback_drain_mirrors_completed_waiter_without_second_agent_turn(
    monkeypatch, tmp_path, terminal_status
):
    import asyncio
    from types import SimpleNamespace

    from core import scheduled_tasks as st
    from storage import messages_service
    from storage.models import agent_sessions, messages
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    engine = create_sqlite_engine()
    metadata.create_all(engine)
    now = messages_service._utc_now_iso()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_vault_waiter",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_vault_waiter",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor_vault_waiter",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_vault_waiter",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="The previous Agent turn completed.",
            native_message_id="agent:previous-result",
        )
        vs.create_secret(
            conn,
            name="WAIT_RESULT_KEY",
            sealed=_sealed("wait-result"),
            protection="protected",
        )
        req = vs.create_access_request(
            conn,
            "WAIT_RESULT_KEY",
            requester={"session_id": "ses_vault_waiter"},
            delivery={"session_id": "ses_vault_waiter"},
        )
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        vs.arm_request_waiter(conn, req["id"], waiter_id="vw_visible", deadline_at=deadline)
        if terminal_status == "approved":
            _grant_from_request(conn, req)
        else:
            vs.deny_request(conn, req["id"])
        vs.complete_request_waiter(conn, req["id"], waiter_id="vw_visible")
        row = _row(conn, req["id"])

    request_store = st.TaskExecutionStore(tmp_path / "task_requests")
    service = st.ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=st.ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    with monkeypatch.context() as failed_mirror:
        failed_mirror.setattr(
            "core.message_mirror.mirror_vault_waiter_outcome",
            lambda **_: False,
        )
        assert service._process_vault_callback_sync(row) == "pending"
        with engine.connect() as conn:
            assert _callback_status(conn, req["id"]) == "pending"

    asyncio.run(service._drain_vault_callbacks())
    # The stable native message id also closes the crash window between mirror + callback mark.
    assert service._process_vault_callback_sync(row) == "sent"

    assert request_store.list_pending() == []
    with engine.connect() as conn:
        assert _callback_status(conn, req["id"]) == "sent"
        [message] = conn.execute(
            select(messages).where(
                messages.c.session_id == "ses_vault_waiter",
                messages.c.type == messages_service.VAULT_TYPE,
            )
        ).mappings().all()
    assert message["author"] == "harness"
    assert message["source"] == "harness"
    assert message["type"] == messages_service.VAULT_TYPE
    assert message["author_name"] == "vault"
    assert json.loads(message["metadata_json"]) == {
        "source_kind": "callback",
        "source_actor": f"vault:{req['id']}",
        "vault_request_type": "access",
        "vault_request_status": terminal_status,
    }

    with engine.connect() as conn:
        inbox = messages_service.list_inbox_sessions(
            conn,
            platform="avibe",
            only_session="ses_vault_waiter",
        )
    assert inbox["sessions"][0]["replied"] is False

    # A later direct/harness input remains unanswered: the Vault outcome is a
    # transcript notification, not terminal evidence for an unrelated turn.
    mirror_harness_inbound(
        MessageContext(
            user_id="scheduled",
            channel_id="ses_vault_waiter",
            platform="avibe",
            message_id="harness:newer-input",
            platform_specific={
                "agent_session_id": "ses_vault_waiter",
                "task_trigger_kind": "watch",
            },
        ),
        "A newer harness input",
    )
    with engine.connect() as conn:
        inbox = messages_service.list_inbox_sessions(
            conn,
            platform="avibe",
            only_session="ses_vault_waiter",
        )
    assert inbox["sessions"][0]["replied"] is True


# --- continuation ownership: a synchronous CLI waiter versus the Session callback -------------


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _start_waiting_vault_run(session_id: str, *, approval_wait: int = 540):
    """A real ``vibe vault run`` that blocks on protected approval, in this test's home."""

    env = dict(os.environ)
    env["AVIBE_SESSION_ID"] = session_id
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_REPO_ROOT), env.get("PYTHONPATH", "")]))
    argv = [
        "vault", "run", "--env", "PROTECTED_KEY", "--approval-wait", str(approval_wait),
        "--", sys.executable, "-c", "pass",
    ]
    return subprocess.Popen(
        [sys.executable, "-c", "import sys; from vibe import cli; sys.argv = ['vibe', *sys.argv[1:]]; cli.main()", *argv],
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_active_waiter(engine, proc) -> dict:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        assert proc.poll() is None, "the waiting vault run exited before arming its waiter"
        with engine.connect() as conn:
            for row in conn.execute(select(vault_requests).where(vault_requests.c.request_type == "access")).mappings():
                waiter = (json.loads(row["delivery"] or "{}") or {}).get("waiter") or {}
                if waiter.get("status") == "active":
                    return dict(row)
        time.sleep(0.05)
    raise AssertionError("the vault run never armed an approval waiter")


def _callback_service(monkeypatch, tmp_path, *, backend="file"):
    from types import SimpleNamespace

    from core import scheduled_tasks as st

    class _Key:
        def to_key(self) -> str:
            return "avibe::scope::ses_owner"

    target = SimpleNamespace(
        session_key=_Key(),
        agent_name="vibey",
        agent_id="aid",
        agent_backend="vibey",
        model=None,
        reasoning_effort=None,
    )
    monkeypatch.setattr(st, "resolve_session_id_target", lambda session_id: target)
    request_store = st.TaskExecutionStore() if backend == "sqlite" else st.TaskExecutionStore(tmp_path / "task_requests")

    def service():
        # A fresh instance per call is a service restart: no in-memory owner state survives it.
        return st.ScheduledTaskService(
            controller=SimpleNamespace(platform_settings_managers={}),
            store=st.ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
            request_store=request_store,
        )

    return service, request_store


@pytest.mark.reap_processes
def test_hard_killed_waiter_hands_a_ready_approval_to_the_callback_before_its_deadline(monkeypatch, tmp_path):
    from vibe import cli

    engine = cli._open_vault_engine()
    with engine.begin() as conn:
        vs.create_secret(conn, name="PROTECTED_KEY", sealed=_sealed("kill"), protection="protected")
    proc = _start_waiting_vault_run("ses_owner")
    try:
        armed = _wait_for_active_waiter(engine, proc)
    finally:
        proc.kill()
        proc.wait(timeout=30)
    waiter = json.loads(armed["delivery"])["waiter"]
    assert datetime.fromisoformat(waiter["deadline_at"]) > datetime.now(timezone.utc) + timedelta(minutes=5)

    with engine.begin() as conn:
        request = vs.get_request(conn, armed["id"], audience=vs.REQUEST_AUDIENCE_AGENT)
        _grant_from_request(conn, request)
        row = _row(conn, armed["id"])
    service, request_store = _callback_service(monkeypatch, tmp_path)

    assert service()._process_vault_callback_sync(row) == "sent"

    [callback_run] = request_store.list_pending()
    assert callback_run.session_id == "ses_owner"
    assert callback_run.source_actor == f"vault:{armed['id']}"
    with engine.connect() as conn:
        delivery = json.loads(_row(conn, armed["id"])["delivery"])
        assert _callback_status(conn, armed["id"]) == "sent"
    assert delivery["waiter"]["status"] == "abandoned"
    assert delivery["continuation"]["run_id"] == callback_run.id


def _arm_lock_waiter(engine, request_id: str, *, deadline_at: str | None = None):
    """Arm a lock-evidenced waiter the way ``vibe vault run`` does; returns its held lock."""

    waiter_id = vs.new_request_waiter_id()
    lock = vs.acquire_request_waiter_lock(engine, waiter_id)
    with engine.begin() as conn:
        vs.arm_request_waiter(
            conn,
            request_id,
            waiter_id=waiter_id,
            deadline_at=deadline_at or (datetime.now(timezone.utc) + timedelta(minutes=9)).isoformat(),
            liveness=vs.WAITER_LIVENESS_LOCK,
        )
    return waiter_id, lock


def _owner_request(conn, name: str, session_id: str = "ses_owner", **kwargs) -> dict:
    vs.create_secret(conn, name=name, sealed=_sealed(name.lower()), protection="protected", **kwargs)
    return vs.create_access_request(conn, name, requester={"session_id": session_id}, delivery={"session_id": session_id})


def _record_mirrors(monkeypatch) -> list[dict]:
    mirrored: list[dict] = []

    def mirror(**kwargs):
        mirrored.append(kwargs)
        return True

    monkeypatch.setattr("core.message_mirror.mirror_vault_waiter_outcome", mirror)
    return mirrored


def test_live_waiter_keeps_its_approval_past_its_wall_clock_deadline_and_across_restart(monkeypatch, tmp_path):
    from config import paths
    from vibe import cli

    engine = cli._open_vault_engine()
    with engine.begin() as conn:
        req = _owner_request(conn, "LIVE_KEY")
    # A suspended machine or a stepped clock: the recorded deadline passed while the owner still waits.
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    waiter_id, lock = _arm_lock_waiter(engine, req["id"], deadline_at=past)
    service, request_store = _callback_service(monkeypatch, tmp_path)
    mirrored = _record_mirrors(monkeypatch)
    try:
        with engine.begin() as conn:
            _grant_from_request(conn, req)
            row = _row(conn, req["id"])
        assert service()._process_vault_callback_sync(row) == "pending"
        assert service()._process_vault_callback_sync(row) == "pending"
        with engine.begin() as conn:
            vs.complete_request_waiter(conn, req["id"], waiter_id=waiter_id)
    finally:
        vs.release_request_waiter_lock(lock, discard=True)

    assert service()._process_vault_callback_sync(row) == "sent"
    assert request_store.list_pending() == []
    assert [entry["request_id"] for entry in mirrored] == [req["id"]]
    with engine.connect() as conn:
        delivery = json.loads(_row(conn, req["id"])["delivery"])
    assert delivery["waiter"]["status"] == "completed"
    assert "continuation" not in delivery
    assert vs.probe_lock_holder(vs.request_waiter_lock_path(paths.get_sqlite_state_path(), waiter_id)) in {"free", "missing"}


@pytest.mark.parametrize("backend", ["sqlite", "file"])
def test_claimed_callback_survives_enqueue_before_mark_and_refuses_a_later_waiter(monkeypatch, tmp_path, backend):
    from vibe import cli

    engine = cli._open_vault_engine()
    with engine.begin() as conn:
        req = _owner_request(conn, "CLAIM_KEY")
        _grant_from_request(conn, req)
        row = _row(conn, req["id"])
    service, request_store = _callback_service(monkeypatch, tmp_path, backend=backend)

    def stopped_before_mark(conn, request_id, *, status):
        raise RuntimeError("the daemon stopped between enqueue and the sent mark")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(vs, "mark_request_callback", stopped_before_mark)
        assert service()._process_vault_callback_sync(row) == "pending"
    [callback_run] = request_store.list_pending()

    # The restarted daemon resumes the claim it recorded instead of deciding (and enqueueing) again.
    assert service()._process_vault_callback_sync(row) == "sent"
    assert [run.id for run in request_store.list_pending()] == [callback_run.id]
    with engine.begin() as conn:
        assert json.loads(_row(conn, req["id"])["delivery"])["continuation"]["run_id"] == callback_run.id
        # A command that only now reaches its waiter must not also continue this approval.
        with pytest.raises(vs.ContinuationClaimedError):
            vs.arm_request_waiter(
                conn,
                req["id"],
                waiter_id=vs.new_request_waiter_id(),
                deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=9)).isoformat(),
                liveness=vs.WAITER_LIVENESS_LOCK,
            )


@pytest.mark.parametrize("case", ["waiter_completed", "sibling_skipped"])
def test_stale_scan_row_cannot_start_a_second_continuation(monkeypatch, tmp_path, case):
    from vibe import cli

    engine = cli._open_vault_engine()
    service, request_store = _callback_service(monkeypatch, tmp_path)
    mirrored = _record_mirrors(monkeypatch)
    with engine.begin() as conn:
        primary = _owner_request(conn, "STALE_A", tags=["deploy"])
        sibling = vs.create_access_request(
            conn, "STALE_A", requester={"session_id": "ses_owner"}, delivery={"session_id": "ses_owner"}
        )
    deadline_at = (datetime.now(timezone.utc) + timedelta(minutes=9)).isoformat()
    if case == "waiter_completed":
        # The scan sees an approved request with no waiter; the command arms and consumes it after.
        with engine.begin() as conn:
            _grant_from_request(conn, primary)
            stale = _row(conn, primary["id"])
            vs.arm_request_waiter(conn, primary["id"], waiter_id="vw_00000000000a", deadline_at=deadline_at)
        processed = primary["id"]
    else:
        with engine.begin() as conn:
            vs.arm_request_waiter(conn, primary["id"], waiter_id="vw_00000000000a", deadline_at=deadline_at)
            _grant_from_request(conn, primary)
            stale = _row(conn, sibling["id"])
        processed = sibling["id"]
    assert stale["callback_status"] == "pending"
    with engine.begin() as conn:
        vs.complete_request_waiter(conn, primary["id"], waiter_id="vw_00000000000a")

    status = service()._process_vault_callback_sync(stale)

    assert request_store.list_pending() == []
    with engine.connect() as conn:
        if case == "waiter_completed":
            assert status == "sent"
            assert [entry["request_id"] for entry in mirrored] == [processed]
        else:
            assert status == "skipped"
            assert mirrored == []
            assert _callback_status(conn, processed) == "skipped"


@pytest.mark.parametrize(
    "owner, deadline_minutes, expected",
    [
        # Released rows keep their recorded deadline: never presumed dead before it.
        ("legacy", 9, "defer"),
        ("legacy", -1, "deliver"),
        # A lock nobody holds proves the owner is gone, whatever its deadline says.
        ("lock_released", 9, "deliver"),
        # A missing lock is not proof of death: the owner keeps its own declared wait bound.
        ("lock_missing", 9, "defer"),
        ("lock_missing", -1, "deliver"),
    ],
)
def test_claim_follows_waiter_owner_evidence(vault, owner, deadline_minutes, expected):
    deadline_at = (datetime.now(timezone.utc) + timedelta(minutes=deadline_minutes)).isoformat()
    with vault.begin() as conn:
        req = _owner_request(conn, "EVIDENCE_KEY")
    if owner == "legacy":
        with vault.begin() as conn:
            vs.arm_request_waiter(conn, req["id"], waiter_id="vw_000000000001", deadline_at=deadline_at)
    else:
        _, lock = _arm_lock_waiter(vault, req["id"], deadline_at=deadline_at)
        vs.release_request_waiter_lock(lock, discard=owner == "lock_missing")
        if owner == "lock_missing":
            # Windows cannot unlink the held handle; make the missing-evidence fixture
            # explicit after release rather than accidentally testing a free lock.
            lock.lock_path.unlink(missing_ok=True)
    with vault.begin() as conn:
        _grant_from_request(conn, req)
        claim = vs.claim_request_callback(conn, req["id"])
        delivery = json.loads(_row(conn, req["id"])["delivery"])

    assert claim.action == expected
    if expected == "deliver":
        assert delivery["waiter"]["status"] == "abandoned"
        assert delivery["continuation"]["run_id"] == claim.run_id
    else:
        assert delivery["waiter"]["status"] == "active"
        assert "continuation" not in delivery


def test_dead_owner_still_waits_for_a_delivery_ready_grant(vault):
    with vault.begin() as conn:
        req = _owner_request(conn, "NOT_READY_KEY")
    waiter_id, lock = _arm_lock_waiter(vault, req["id"])
    vs.release_request_waiter_lock(lock, discard=False)
    with vault.begin() as conn:
        grant = _grant_from_request_with_cache_ready(conn, req, cache_ready=False)
        assert vs.claim_request_callback(conn, req["id"]).action == "defer"
        assert "continuation" not in json.loads(_row(conn, req["id"])["delivery"])

        vs.mark_grant_agent_ready(conn, grant["id"])
        claim = vs.claim_request_callback(conn, req["id"])

    assert claim.action == "deliver"
    assert claim.released_waiter_id == waiter_id


@pytest.mark.parametrize("owner_live, expected", [(True, "defer"), (False, "deliver")])
def test_covered_sibling_follows_the_grant_owner_waiter(vault, owner_live, expected):
    with vault.begin() as conn:
        primary = _owner_request(conn, "SIB_A", tags=["deploy"])
        sibling = vs.create_access_request(
            conn, "SIB_A", requester={"session_id": "ses_owner"}, delivery={"session_id": "ses_owner"}
        )
    _, lock = _arm_lock_waiter(vault, primary["id"])
    try:
        if not owner_live:
            vs.release_request_waiter_lock(lock, discard=False)
        with vault.begin() as conn:
            _grant_from_request(conn, primary)
            assert vs.claim_request_callback(conn, sibling["id"]).action == expected
    finally:
        if owner_live:
            vs.release_request_waiter_lock(lock, discard=False)
