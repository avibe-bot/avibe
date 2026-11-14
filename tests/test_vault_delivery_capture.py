"""Delivery evidence must describe the envelope actually handed to custody.

The existing delivery tests have no concurrent replacement between reads. A pins
one capture's SQLite snapshot; B exercises the real fetch/run/inject callers and
would expose a replacement ACL after an otherwise successful resolution. Custody
is mocked, never the capture, grant selection or audit behavior being asserted.
"""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from storage import resource_access_service as ras, vault_service as vs
from storage.models import vault_audit, vault_grants, vault_secrets
from storage.vault_crypto import Sealed
from tests.test_vault_cli import _ns
from vibe import api, cli

pytestmark = pytest.mark.usefixtures("in_process_cli_control")


def _envelope(name, generation="old", field="value"):
    return Sealed(f"ct-{name}-{generation}-{field}", "synthetic-nonce", "synthetic-wrap")


def _create(conn, name, *, generation="old", protection="standard", one_shot=False):
    vs.create_secret(
        conn, name=name, sealed=_envelope(name, generation), protection=protection,
        fields=[{"name": "username", "sealed": _envelope(name, generation, "username")}],
        policy={"allowed_hosts": ["example.test" if generation == "old" else "replacement.test"],
                "auth": {"type": "bearer"}, "always_ask": one_shot},
    )
    identifier = conn.execute(select(vault_secrets.c.id).where(vault_secrets.c.name == name)).scalar_one()
    ras.ensure_resource_policy(
        conn, resource_kind="vault_secret", resource_id=identifier,
        organization_id="org", owner_user_id="other", group_ids=[],
        access_level="private" if generation == "old" else "public",
    )
    return identifier


@pytest.fixture
def delivery_db(monkeypatch):
    # Every state path is redirected by conftest. Explicitly guard both custody
    # entry points as well: an unmocked egress/cleanup must fail this test.
    def no_custody(*args, **kwargs):
        pytest.fail("real custody is forbidden in delivery capture tests")

    monkeypatch.setattr(api, "_require_avault_path", no_custody)
    monkeypatch.setattr(api, "_avault_agent_manager", no_custody)
    monkeypatch.setattr(api, "avault_agent_release", Mock(return_value={"released": True}))
    vs.GRANT_RUNTIME_CACHE.clear()
    engine = cli._open_vault_engine()
    yield engine
    vs.GRANT_RUNTIME_CACHE.clear()


def _grant(conn, refs, purpose):
    request = vs.create_access_request(conn, source_selector={"env": refs}, purpose=purpose)
    option = request["card"]["grant_options"][0]
    return vs.create_grant(
        conn, member_names=option["member_snapshot"], source_selector=option["source_selector"],
        purpose=purpose, request_id=request["id"],
    )


def _reader():
    return ras.ResourceUserContext(
        subject="reader", organization_id="org", organization_member_id="member",
        organization_role="member", instance_role="editor", is_remote=False,
        instance_access_source="organization_group",
    )


def test_capture_keeps_identity_envelope_and_acl_in_one_snapshot(delivery_db, monkeypatch):
    """A: another real connection replaces the name between record and ACL reads."""
    with delivery_db.begin() as conn:
        original_id = _create(conn, "KEY")
    resolve = vs._resolve_record_ref
    replaced = []

    def interleave(conn, ref, **kwargs):
        result = resolve(conn, ref, **kwargs)
        if not replaced:
            with delivery_db.begin() as other:
                vs.delete_secret(other, "KEY")
                replaced.append(_create(other, "KEY", generation="new"))
        return result

    monkeypatch.setattr(vs, "_resolve_record_ref", interleave)
    with delivery_db.connect() as conn:
        captures = vs.capture_deliveries(conn, ["KEY", "KEY.username"])
    assert replaced and replaced[0] != original_id
    for captured, field in zip(captures, ["value", "username"]):
        resource = captured.identity.access_snapshot["resources"][0]
        assert captured.envelope == _envelope("KEY", field=field)
        assert captured.identity.secret_id == resource["resource_id"] == resource["policy"]["resource_id"] == original_id
        assert resource["policy"]["access_level"] == "private"
        assert captured.metadata["policy"]["allowed_hosts"] == ["example.test"]


@pytest.mark.parametrize("proof", [2, 3])
@pytest.mark.parametrize("mode", ["fetch", "run", "inject"])
@pytest.mark.parametrize("protection,one_shot,shared", [
    ("standard", False, False),
    ("standard", True, True),
    ("protected", False, True),
    ("protected", True, False),
])
def test_delivery_keeps_resolved_evidence_after_name_replacement(
    delivery_db, monkeypatch, tmp_path, capfd, mode, protection, one_shot, shared, proof,
):
    """B: replace after resolution; inline and reference evidence must retain identity."""
    monkeypatch.setattr("storage.compatibility.VERIFIED_PREDECESSOR_EPOCH", proof)
    names = ["KEY", "OTHER"] if shared and mode != "fetch" else ["KEY"]
    refs = [record for name in names for record in (name, f"{name}.username")]
    grant = None
    with delivery_db.begin() as conn:
        original_ids = {name: _create(conn, name, protection=protection, one_shot=one_shot) for name in names}
        if protection == "protected" or one_shot:
            grant = _grant(conn, refs, mode)

    resolver_name = "_resolve_single_vault_delivery" if mode == "fetch" else f"_resolve_vault_{mode}_delivery"
    resolve = getattr(cli, resolver_name)
    replacements = {}

    def replace_after_resolution(*args, **kwargs):
        result = resolve(*args, **kwargs)
        with delivery_db.begin() as conn:
            for name in names:
                vs.delete_secret(conn, name)
                replacements[name] = _create(conn, name, generation="new")
        return result

    monkeypatch.setattr(cli, resolver_name, replace_after_resolution)
    handed = []

    def handoff(*args, **kwargs):
        if mode == "fetch":
            envelope = kwargs["sealed"] if kwargs else args[1]
            request = kwargs["request"] if kwargs else args[2]
            handed.append(("KEY.username", envelope))
            assert request["allowed_hosts"] == ["example.test"]
            return {"status": 200, "headers": {}, "body": "ok"}
        records = kwargs["secrets"] if kwargs else args[0 if mode == "run" else 2]
        handed.extend((record["name"], record["envelope"]) for record in records)
        # Audit identities never become custody wire fields.
        assert all(set(record) <= {"name", "env", "key", "envelope", "tier"} for record in records)
        return {"exit_code": 0, "delivered": True} if mode == "run" else None

    deliver = Mock(side_effect=handoff)
    monkeypatch.setattr(api, f"avault_deliver_{mode}", deliver)
    monkeypatch.setattr(api, f"avault_agent_deliver_{mode}", deliver)
    args = _ns(
        auth="KEY.username", url="https://example.test/path", method="GET", output=None,
        env=refs, command_argv=["python3", "-c", "pass"], out=str(tmp_path / "output.env"), format="dotenv",
    )
    assert getattr(cli, f"cmd_vault_{mode}")(args) == 0, capfd.readouterr()
    deliver.assert_called_once()
    expected_refs = ["KEY.username"] if mode == "fetch" else refs
    assert handed == [(ref, _envelope(ref.split(".")[0], field=ref.split(".")[1] if "." in ref else "value")) for ref in expected_refs]
    with delivery_db.connect() as conn:
        events = [dict(row) for row in conn.execute(select(vault_audit).where(
            vault_audit.c.event == ("proxied" if mode == "fetch" else "delivered"),
        )).mappings()]
        assert len(events) == len(names)
        snapshots = vs._load_audit_access_snapshots(conn, events)
        for row in events:
            raw = json.loads(row["delivery"])["_resource_access_snapshot"]
            assert ("ref" in raw) == (mode == "fetch" and proof == 3)
            present, snapshot = vs._audit_access_snapshot(row, snapshots=snapshots)
            assert present and snapshot is not None
            resource = snapshot["resources"][0]
            assert resource["resource_id"] == resource["policy"]["resource_id"] == original_ids[row["secret_name"]]
            assert resource["policy"]["access_level"] == "private"
        # Replacement visibility must not grant access to the original private event.
        assert not [row for row in vs.list_audit(conn, user_context=_reader()) if row["event"] in {"proxied", "delivered"}]
        assert all(row == 0 for row in conn.execute(select(vault_secrets.c.use_count)).scalars())
        if grant is not None:
            assert conn.execute(select(vault_grants.c.status).where(vault_grants.c.id == grant["id"])).scalar_one() == "expired"
    assert all(original_ids[name] != replacements[name] for name in names)


@pytest.mark.parametrize("mode", ["run", "inject"])
@pytest.mark.parametrize("change", ["identity", "acl", "envelope", "fetch_policy"])
def test_batch_rejects_changed_preflight_before_grant_resolution(delivery_db, monkeypatch, tmp_path, mode, change):
    """Captured batches cannot select a replacement's grants or silently refresh evidence."""
    with delivery_db.begin() as conn:
        original_id = _create(conn, "KEY")
    capture = cli._capture_vault_batch

    def change_after_capture(*args):
        result = capture(*args)
        with delivery_db.begin() as conn:
            if change == "identity":
                vs.delete_secret(conn, "KEY")
                # Even identical ciphertext and policy cannot replace immutable identity.
                assert _create(conn, "KEY") != original_id
            elif change == "acl":
                ras.apply_control_plane_intent(conn, organization_id="org", resource_kind="vault_secret",
                    resource_id=original_id, revision=1, access_level="public", group_ids=[])
            elif change == "envelope":
                vs.rotate_secret(conn, "KEY", sealed=_envelope("KEY", "new"))
            else:
                vs.update_secret_metadata(conn, "KEY", policy={"allowed_hosts": ["changed.test"], "auth": {"type": "bearer"}})
        return result

    monkeypatch.setattr(cli, "_capture_vault_batch", change_after_capture)
    with pytest.raises(vs.SecretConflictError):
        if mode == "run":
            cli._resolve_vault_run_delivery(delivery_db, {"KEY": "KEY"}, ["python3"], args=_ns())
        else:
            cli._resolve_vault_inject_delivery(delivery_db, {"KEY": "KEY"}, path=str(tmp_path / "out"), fmt="dotenv", args=_ns())


def test_fetch_final_policy_belongs_to_resolved_envelope(delivery_db, monkeypatch, capfd):
    """A policy edit after early preflight cannot authorize the subsequently selected envelope."""
    with delivery_db.begin() as conn:
        _create(conn, "KEY")
    resolve = cli._resolve_single_vault_delivery

    def change_before_resolution(*args, **kwargs):
        with delivery_db.begin() as conn:
            vs.update_secret_metadata(conn, "KEY", policy={"allowed_hosts": ["replacement.test"], "auth": {"type": "bearer"}})
        return resolve(*args, **kwargs)

    monkeypatch.setattr(cli, "_resolve_single_vault_delivery", change_before_resolution)
    deliver = Mock(return_value={"status": 200, "headers": {}, "body": "ok"})
    monkeypatch.setattr(api, "avault_deliver_fetch", deliver)
    assert cli.cmd_vault_fetch(_ns(auth="KEY", url="https://example.test/path", method="GET", output=None)) == 1
    assert json.loads(capfd.readouterr().err)["code"] == "host_not_allowed"
    deliver.assert_not_called()


@pytest.mark.parametrize("protection", ["standard", "protected"])
def test_single_capture_failure_rolls_back_one_shot_reservation(delivery_db, monkeypatch, protection):
    """Capture faults must not strand the one-shot grant reserved in this transaction."""
    with delivery_db.begin() as conn:
        _create(conn, "KEY", protection=protection, one_shot=True)
        grant = _grant(conn, ["KEY"], "fetch")

    def capture_fails(*args, **kwargs):
        raise RuntimeError("synthetic capture failure")

    monkeypatch.setattr(vs, "capture_deliveries", capture_fails)
    with pytest.raises(RuntimeError, match="synthetic capture failure"):
        cli._resolve_single_vault_delivery(delivery_db, "KEY", requester={}, delivery={}, purpose="fetch")
    with delivery_db.connect() as conn:
        assert conn.execute(select(vault_grants.c.status).where(vault_grants.c.id == grant["id"])).scalar_one() == "active"
        assert conn.execute(select(vault_audit.c.id).where(
            vault_audit.c.grant_id == grant["id"], vault_audit.c.event == "grant-reserved-one-shot",
        )).first() is None
