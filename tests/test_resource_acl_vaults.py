from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from storage import resource_access_service, vault_service
from storage.db import create_sqlite_engine
from storage.models import (
    metadata,
    resource_access_groups,
    resource_access_policies,
    state_meta,
    vault_grants,
    vault_requests,
    vault_secrets,
)
from storage.vault_crypto import Sealed
from vibe import api, remote_access


def _context(
    subject: str,
    *,
    group_ids: frozenset[str] | None = frozenset({"group-engineering"}),
) -> resource_access_service.ResourceUserContext:
    return resource_access_service.ResourceUserContext(
        subject=subject,
        email=f"{subject}@example.com",
        organization_id="org-1",
        organization_member_id=f"member-{subject}",
        organization_role="member",
        group_ids=group_ids,
        instance_role="viewer",
        instance_access_source="organization_group",
        is_remote=True,
    )


@pytest.fixture
def vault(tmp_path):
    vault_service.GRANT_RUNTIME_CACHE.clear()
    engine = create_sqlite_engine(tmp_path / "vault_acl.sqlite")
    metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _sealed(name: str) -> Sealed:
    return Sealed(ciphertext=f"ciphertext-{name}", nonce=f"nonce-{name}", wrap_meta=f"wrap-{name}")


def _create_secret(conn, name: str, **kwargs) -> None:
    vault_service.create_secret(conn, name=name, sealed=_sealed(name), **kwargs)


def _secret_id(conn, name: str) -> str:
    return str(conn.execute(select(vault_secrets.c.id).where(vault_secrets.c.name == name)).scalar_one())


def _set_policy(
    conn,
    name: str,
    *,
    access_level: str,
    group_ids: list[str] | None = None,
    owner_user_id: str = "owner-1",
) -> str:
    resource_id = _secret_id(conn, name)
    resource_access_service.ensure_resource_policy(
        conn,
        resource_kind="vault_secret",
        resource_id=resource_id,
        organization_id="org-1",
        owner_user_id=owner_user_id,
        access_level=access_level,
        group_ids=group_ids,
        policy_revision=1,
        last_applied_control_plane_revision=1,
    )
    return resource_id


def _grant_from_request(conn, request: dict, *, user_context) -> dict:
    option = request["card"]["grant_options"][0]
    return vault_service.create_grant(
        conn,
        member_names=option["member_snapshot"],
        source_selector=option["source_selector"],
        purpose=option["purpose"],
        request_id=request["id"],
        user_context=user_context,
    )


def test_vault_list_filters_acl_rows_without_returning_envelopes(vault) -> None:
    with vault.begin() as conn:
        _create_secret(conn, "PRIVATE_KEY")
        _create_secret(conn, "PUBLIC_KEY")
        _create_secret(conn, "SCOPED_KEY")
        _set_policy(conn, "PRIVATE_KEY", access_level="private")
        _set_policy(conn, "PUBLIC_KEY", access_level="public")
        _set_policy(conn, "SCOPED_KEY", access_level="scope", group_ids=["group-engineering"])

    with vault.connect() as conn:
        owner_rows = vault_service.list_secrets(conn, user_context=_context("owner-1"))
        member_rows = vault_service.list_secrets(conn, user_context=_context("member-1"))
        no_group_rows = vault_service.list_secrets(conn, user_context=_context("member-2", group_ids=None))

    assert {row["name"] for row in owner_rows} == {"PRIVATE_KEY", "PUBLIC_KEY", "SCOPED_KEY"}
    assert {row["name"] for row in member_rows} == {"PUBLIC_KEY", "SCOPED_KEY"}
    assert {row["name"] for row in no_group_rows} == {"PUBLIC_KEY"}
    serialized = json.dumps(member_rows)
    assert "ciphertext-" not in serialized
    assert "nonce-" not in serialized
    assert "wrap-" not in serialized
    assert "ciphertext" not in serialized
    assert "wrap_meta" not in serialized


def test_inaccessible_vault_requests_and_grants_fail_before_mutating_state(vault) -> None:
    owner = _context("owner-1")
    member = _context("member-1")
    with vault.begin() as conn:
        _create_secret(conn, "PRIVATE_ACCESS", protection="protected")
        _create_secret(conn, "PRIVATE_SIGN", kind="keypair", signer_kind="local")
        _set_policy(conn, "PRIVATE_ACCESS", access_level="private")
        _set_policy(conn, "PRIVATE_SIGN", access_level="private")
        owner_request = vault_service.create_access_request(conn, "PRIVATE_ACCESS", user_context=owner)

        with pytest.raises(vault_service.VaultSecretAccessError):
            vault_service.create_access_request(conn, "PRIVATE_ACCESS", user_context=member)
        with pytest.raises(vault_service.VaultSecretAccessError):
            vault_service.create_sign_request(
                conn,
                "PRIVATE_SIGN",
                digest="00" * 32,
                scheme="ecdsa-secp256k1-recoverable",
                user_context=member,
            )

        option = owner_request["card"]["grant_options"][0]
        with pytest.raises(vault_service.VaultSecretAccessError):
            vault_service.create_grant(
                conn,
                member_names=option["member_snapshot"],
                source_selector=option["source_selector"],
                purpose=option["purpose"],
                request_id=owner_request["id"],
                user_context=member,
            )

        requests = list(conn.execute(select(vault_requests)).mappings())
        grants = list(conn.execute(select(vault_grants)).mappings())

    assert [row["id"] for row in requests] == [owner_request["id"]]
    assert requests[0]["status"] == "pending"
    assert grants == []


def test_vault_request_reads_and_denial_enforce_member_secret_acls(vault) -> None:
    owner = _context("owner-1")
    member = _context("member-1")
    with vault.begin() as conn:
        _create_secret(conn, "PRIVATE_REQUEST", protection="protected")
        _create_secret(conn, "PUBLIC_REQUEST", protection="protected")
        _set_policy(conn, "PRIVATE_REQUEST", access_level="private")
        _set_policy(conn, "PUBLIC_REQUEST", access_level="public")
        private_request = vault_service.create_access_request(conn, "PRIVATE_REQUEST", user_context=owner)
        public_request = vault_service.create_access_request(conn, "PUBLIC_REQUEST", user_context=owner)

        visible = vault_service.list_requests(conn, user_context=member)
        with pytest.raises(vault_service.VaultSecretAccessError):
            vault_service.get_request(conn, private_request["id"], user_context=member)
        with pytest.raises(vault_service.VaultSecretAccessError):
            vault_service.deny_request(conn, private_request["id"], user_context=member)
        with pytest.raises(vault_service.VaultSecretAccessError):
            vault_service.deny_request(conn, public_request["id"], user_context=member)

        statuses_before_owner_decision = dict(
            conn.execute(select(vault_requests.c.id, vault_requests.c.status)).all()
        )
        owner_payload = vault_service.get_request(conn, private_request["id"], user_context=owner)
        denied = vault_service.deny_request(conn, private_request["id"], user_context=owner)

    assert [request["id"] for request in visible] == [public_request["id"]]
    assert "secret_unlock_material" in owner_payload["card"]
    assert statuses_before_owner_decision == {
        private_request["id"]: "pending",
        public_request["id"]: "pending",
    }
    assert denied["status"] == "denied"


def test_vault_request_limit_applies_after_acl_filtering(vault) -> None:
    owner = _context("owner-1")
    member = _context("member-1")
    with vault.begin() as conn:
        _create_secret(conn, "VISIBLE_REQUEST", protection="protected")
        _create_secret(conn, "HIDDEN_REQUEST", protection="protected")
        _set_policy(conn, "VISIBLE_REQUEST", access_level="public")
        _set_policy(conn, "HIDDEN_REQUEST", access_level="private")
        visible_request = vault_service.create_access_request(conn, "VISIBLE_REQUEST", user_context=owner)
        vault_service.create_access_request(conn, "HIDDEN_REQUEST", user_context=owner)

        visible = vault_service.list_requests(conn, limit=1, user_context=member)

    assert [request["id"] for request in visible] == [visible_request["id"]]


def test_vault_grant_reads_and_revocation_enforce_all_member_secret_acls(vault) -> None:
    owner = _context("owner-1")
    member = _context("member-1")
    with vault.begin() as conn:
        _create_secret(conn, "OWNER_GRANT", protection="protected")
        _create_secret(conn, "MEMBER_GRANT", protection="protected")
        _set_policy(conn, "OWNER_GRANT", access_level="private")
        _set_policy(conn, "MEMBER_GRANT", access_level="private", owner_user_id="member-1")
        owner_request = vault_service.create_access_request(conn, "OWNER_GRANT", user_context=owner)
        member_request = vault_service.create_access_request(conn, "MEMBER_GRANT", user_context=member)
        owner_grant = _grant_from_request(conn, owner_request, user_context=owner)
        member_grant = _grant_from_request(conn, member_request, user_context=member)

        visible = vault_service.list_grants(conn, user_context=member)
        with pytest.raises(vault_service.VaultSecretAccessError):
            vault_service.revoke_grant(conn, owner_grant["id"], user_context=member)
        owner_status = conn.execute(
            select(vault_grants.c.status).where(vault_grants.c.id == owner_grant["id"])
        ).scalar_one()
        revoked = vault_service.revoke_grant(conn, member_grant["id"], user_context=member)

    assert [grant["id"] for grant in visible] == [member_grant["id"]]
    assert owner_status == "active"
    assert revoked["status"] == "revoked"


def test_vault_audit_filters_secret_request_and_grant_metadata_before_limit(vault) -> None:
    owner = _context("owner-1")
    member = _context("member-1")
    with vault.begin() as conn:
        _create_secret(conn, "OWNER_AUDIT", protection="protected")
        _create_secret(conn, "MEMBER_AUDIT", protection="protected")
        _set_policy(conn, "OWNER_AUDIT", access_level="private")
        _set_policy(conn, "MEMBER_AUDIT", access_level="private", owner_user_id="member-1")
        owner_request = vault_service.create_access_request(conn, "OWNER_AUDIT", user_context=owner)
        owner_grant = _grant_from_request(conn, owner_request, user_context=owner)
        vault_service.audit(conn, "member-visible", secret_name="MEMBER_AUDIT")
        vault_service.audit(
            conn,
            "owner-hidden",
            request_id=owner_request["id"],
            grant_id=owner_grant["id"],
            delivery={"grant_id": owner_grant["id"]},
        )

        visible = vault_service.list_audit(conn, limit=1, user_context=member)

    assert [row["event"] for row in visible] == ["member-visible"]
    assert owner_request["id"] not in json.dumps(visible)
    assert owner_grant["id"] not in json.dumps(visible)


def test_vault_api_grant_and_audit_endpoints_use_current_resource_context(vault, monkeypatch) -> None:
    owner = _context("owner-1")
    member = _context("member-1")
    with vault.begin() as conn:
        _create_secret(conn, "API_OWNER", protection="protected")
        _create_secret(conn, "API_MEMBER", protection="protected")
        _set_policy(conn, "API_OWNER", access_level="private")
        _set_policy(conn, "API_MEMBER", access_level="private", owner_user_id="member-1")
        owner_request = vault_service.create_access_request(conn, "API_OWNER", user_context=owner)
        member_request = vault_service.create_access_request(conn, "API_MEMBER", user_context=member)
        owner_grant = _grant_from_request(conn, owner_request, user_context=owner)
        member_grant = _grant_from_request(conn, member_request, user_context=member)
        vault_service.audit(conn, "api-member-visible", secret_name="API_MEMBER")

    releases: list[list[dict[str, str]]] = []
    monkeypatch.setattr(api, "_vault_engine", lambda: vault)
    monkeypatch.setattr(api, "resolve_resource_access_context", lambda: member)
    monkeypatch.setattr(
        api,
        "release_vault_agent_scopes",
        lambda scopes, *, reason: releases.append([dict(scope) for scope in scopes]),
    )

    grants = api.get_vault_grants()["grants"]
    audit_rows = api.get_vault_audit()["events"]
    with pytest.raises(api.VaultApiError) as exc:
        api.revoke_vault_grant(owner_grant["id"])

    with vault.connect() as conn:
        owner_status = conn.execute(
            select(vault_grants.c.status).where(vault_grants.c.id == owner_grant["id"])
        ).scalar_one()

    assert [grant["id"] for grant in grants] == [member_grant["id"]]
    assert "api-member-visible" in {row["event"] for row in audit_rows}
    assert owner_grant["id"] not in json.dumps(audit_rows)
    assert exc.value.status == 403
    assert owner_status == "active"
    assert releases == []


@pytest.mark.parametrize(
    "selector",
    [
        {"env": ["PRIVATE_SELECTOR"]},
        {"tags": ["deploy"]},
        {"skills": ["release"]},
    ],
)
def test_cli_selector_resolution_rejects_inaccessible_secrets(vault, selector: dict) -> None:
    with vault.begin() as conn:
        _create_secret(conn, "PRIVATE_SELECTOR", tags=["deploy", "skill:release"])
        _set_policy(conn, "PRIVATE_SELECTOR", access_level="private")
        with pytest.raises(vault_service.VaultSecretAccessError):
            vault_service.expand_value_delivery_selector(
                conn,
                user_context=_context("member-1"),
                **selector,
            )
        assert list(conn.execute(select(vault_requests)).mappings()) == []
        assert list(conn.execute(select(vault_grants)).mappings()) == []


def test_remote_created_secret_registers_private_organization_policy(vault) -> None:
    creator = _context("member-1")
    with vault.begin() as conn:
        _create_secret(conn, "REMOTE_CREATED", user_context=creator)
        policy = resource_access_service.get_resource_policy(
            "vault_secret",
            _secret_id(conn, "REMOTE_CREATED"),
            connection=conn,
        )

    assert policy is not None
    assert policy["organization_id"] == "org-1"
    assert policy["owner_user_id"] == "member-1"
    assert policy["access_level"] == "private"


def test_vault_secret_removal_deletes_resource_policy_and_groups(vault) -> None:
    with vault.begin() as conn:
        _create_secret(conn, "REMOVED_SECRET")
        resource_id = _set_policy(
            conn,
            "REMOVED_SECRET",
            access_level="scope",
            group_ids=["group-engineering"],
        )

        vault_service.delete_secret(conn, "REMOVED_SECRET", user_context=_context("owner-1"))
        policies = conn.execute(
            select(resource_access_policies).where(
                resource_access_policies.c.resource_kind == "vault_secret",
                resource_access_policies.c.resource_id == resource_id,
            )
        ).all()
        groups = conn.execute(
            select(resource_access_groups).where(
                resource_access_groups.c.resource_kind == "vault_secret",
                resource_access_groups.c.resource_id == resource_id,
            )
        ).all()

    assert policies == []
    assert groups == []


def test_public_vault_use_does_not_grant_management(vault) -> None:
    with vault.begin() as conn:
        _create_secret(conn, "PUBLIC_MANAGEMENT")
        _set_policy(conn, "PUBLIC_MANAGEMENT", access_level="public")

        assert vault_service.get_secret_meta(
            conn,
            "PUBLIC_MANAGEMENT",
            user_context=_context("member-1"),
        )["name"] == "PUBLIC_MANAGEMENT"
        with pytest.raises(vault_service.VaultSecretAccessError):
            vault_service.update_secret_metadata(
                conn,
                "PUBLIC_MANAGEMENT",
                description="not allowed",
                user_context=_context("member-1"),
            )
        with pytest.raises(vault_service.VaultSecretAccessError):
            vault_service.delete_secret(
                conn,
                "PUBLIC_MANAGEMENT",
                user_context=_context("member-1"),
            )

        updated = vault_service.update_secret_metadata(
            conn,
            "PUBLIC_MANAGEMENT",
            description="owner update",
            user_context=_context("owner-1"),
        )

    assert updated["description"] == "owner update"


def test_remote_external_guest_cannot_create_vault_secret(vault) -> None:
    guest = resource_access_service.ResourceUserContext(
        subject="guest-1",
        instance_role="viewer",
        instance_access_source="email",
        is_remote=True,
    )

    with vault.begin() as conn:
        with pytest.raises(vault_service.VaultSecretAccessError):
            _create_secret(conn, "GUEST_CREATED", user_context=guest)
        assert conn.execute(select(vault_secrets).where(vault_secrets.c.name == "GUEST_CREATED")).first() is None


@pytest.mark.parametrize(
    ("initial_level", "initial_groups", "updated_level", "updated_groups"),
    [
        ("public", None, "scope", ["group-engineering"]),
        ("scope", ["group-engineering", "group-sales"], "scope", ["group-engineering"]),
    ],
)
def test_narrowed_vault_policy_revokes_active_grants(
    vault,
    monkeypatch,
    initial_level: str,
    initial_groups: list[str] | None,
    updated_level: str,
    updated_groups: list[str],
) -> None:
    owner = _context("owner-1")
    with vault.begin() as conn:
        _create_secret(conn, "NARROWED_KEY", protection="protected")
        resource_id = _set_policy(
            conn,
            "NARROWED_KEY",
            access_level=initial_level,
            group_ids=initial_groups,
        )
        request = vault_service.create_access_request(conn, "NARROWED_KEY", user_context=owner)
        grant = _grant_from_request(conn, request, user_context=owner)

    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: vault)
    monkeypatch.setattr(
        remote_access,
        "publish_resource_index",
        lambda *_args, **_kwargs: {"organization_id": "org-1", "resources": []},
    )
    monkeypatch.setattr(
        remote_access,
        "pull_resource_acl_intents",
        lambda *_args, **_kwargs: {
            "organization_id": "org-1",
            "intents": [
                {
                    "resource_kind": "vault_secret",
                    "resource_id": resource_id,
                    "revision": 2,
                    "access_level": updated_level,
                    "group_ids": updated_groups,
                }
            ],
        },
    )
    monkeypatch.setattr(remote_access, "acknowledge_resource_acl_intent", lambda *_args, **_kwargs: {})
    releases: list[dict] = []
    monkeypatch.setattr(
        remote_access.api,
        "release_vault_agent_scopes",
        lambda scopes, *, reason: releases.append({"scopes": scopes, "reason": reason}),
    )

    result = remote_access._sync_one_organization(None, organization_id="org-1", resources=[])

    assert result["applied"] == 1
    with vault.connect() as conn:
        status = conn.execute(select(vault_grants.c.status).where(vault_grants.c.id == grant["id"])).scalar_one()
    assert status == "revoked"
    assert releases == [{"scopes": [{"grant_id": grant["id"]}], "reason": "resource-access-policy-narrowed"}]


def test_narrowed_vault_release_failure_stays_pending_until_retry_succeeds(vault, monkeypatch) -> None:
    owner = _context("owner-1")
    with vault.begin() as conn:
        _create_secret(conn, "RETRY_RELEASE_KEY", protection="protected")
        resource_id = _set_policy(conn, "RETRY_RELEASE_KEY", access_level="public")
        request = vault_service.create_access_request(conn, "RETRY_RELEASE_KEY", user_context=owner)
        grant = _grant_from_request(conn, request, user_context=owner)

    intent = {
        "resource_kind": "vault_secret",
        "resource_id": resource_id,
        "revision": 2,
        "access_level": "private",
        "group_ids": [],
    }
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: vault)
    monkeypatch.setattr(
        remote_access,
        "publish_resource_index",
        lambda *_args, **_kwargs: {"organization_id": "org-1", "resources": []},
    )
    monkeypatch.setattr(
        remote_access,
        "pull_resource_acl_intents",
        lambda *_args, **_kwargs: {"organization_id": "org-1", "intents": [intent]},
    )
    acknowledgements: list[dict] = []
    monkeypatch.setattr(
        remote_access,
        "acknowledge_resource_acl_intent",
        lambda *_args, **kwargs: acknowledgements.append(kwargs),
    )
    releases: list[list[dict[str, str]]] = []

    def release(scopes, *, reason):
        releases.append([dict(scope) for scope in scopes])
        if len(releases) == 1:
            raise RuntimeError("resident release failed")

    monkeypatch.setattr(remote_access.api, "release_vault_agent_scopes", release)

    first = remote_access._sync_one_organization(None, organization_id="org-1", resources=[])
    pending_key = remote_access._pending_vault_release_key("org-1", resource_id, 2)
    with vault.connect() as conn:
        pending_after_failure = conn.execute(
            select(state_meta.c.value_json).where(state_meta.c.key == pending_key)
        ).scalar_one_or_none()

    second = remote_access._sync_one_organization(None, organization_id="org-1", resources=[])
    with vault.connect() as conn:
        pending_after_success = conn.execute(
            select(state_meta.c.value_json).where(state_meta.c.key == pending_key)
        ).scalar_one_or_none()

    assert first["ok"] is False
    assert first["applied"] == 1
    assert first["acknowledged"] == 0
    assert first["ack_errors"] == 1
    assert pending_after_failure is not None
    assert second["ok"] is True
    assert second["acknowledged"] == 1
    assert pending_after_success is None
    assert releases == [[{"grant_id": grant["id"]}], [{"grant_id": grant["id"]}]]
    assert acknowledgements == [
        {
            "resource_kind": "vault_secret",
            "resource_id": resource_id,
            "revision": 2,
            "outcome": "applied",
        }
    ]
