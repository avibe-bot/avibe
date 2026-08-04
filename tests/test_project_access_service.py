from __future__ import annotations

import sqlalchemy as sa

from storage import project_access_service, projects_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import project_access_bindings, project_access_policies
from vibe.authorization import AuthorizationContext, trusted_local_context


def _context(
    role: str,
    *,
    email: str = "member@example.com",
    organization_id: str | None = None,
    group_ids: tuple[str, ...] = (),
) -> AuthorizationContext:
    return AuthorizationContext(
        instance_role=role,
        email=email,
        organization_id=organization_id,
        group_ids=frozenset(group_ids),
        is_remote=True,
    )


def _engine_with_project(tmp_path):
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    folder = tmp_path / "project"
    folder.mkdir()
    with engine.begin() as conn:
        project = projects_service.create_project(conn, str(folder), display_name="Project One")
    return engine, project


def _intent(project_id: str, revision: int, *, mode="restricted", bindings=None):
    return {
        "project_id": project_id,
        "revision": revision,
        "mode": mode,
        "bindings": bindings or [],
    }


def test_migration_creates_project_policy_schema(tmp_path) -> None:
    engine, _project = _engine_with_project(tmp_path)
    inspector = sa.inspect(engine)

    assert "project_access_policies" in inspector.get_table_names()
    assert "project_access_bindings" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("project_access_policies")} >= {
        "project_id",
        "scope_id",
        "organization_id",
        "mode",
        "policy_revision",
        "last_applied_control_plane_revision",
    }
    foreign_keys = inspector.get_foreign_keys("project_access_bindings")
    assert foreign_keys[0]["referred_table"] == "project_access_policies"
    assert foreign_keys[0]["options"]["ondelete"] == "CASCADE"


def test_missing_and_inherit_policies_preserve_instance_role(tmp_path) -> None:
    engine, project = _engine_with_project(tmp_path)
    editor = _context("editor")

    with engine.begin() as conn:
        assert project_access_service.get_effective_project_role(conn, editor, project["id"]) == "editor"
        result = project_access_service.apply_project_access_intent(
            conn,
            _intent(project["id"], 1, mode="inherit"),
        )
        assert result.outcome == "applied"
        assert project_access_service.get_effective_project_role(conn, editor, project["id"]) == "editor"


def test_restricted_policy_matches_email_domain_and_group(tmp_path) -> None:
    engine, project = _engine_with_project(tmp_path)
    intent = _intent(
        project["id"],
        1,
        bindings=[
            {
                "principal_kind": "email",
                "principal_value": "VIEWER@example.com",
                "access_role": "viewer",
            },
            {
                "principal_kind": "email_domain",
                "principal_value": "Example.com",
                "access_role": "editor",
            },
            {
                "principal_kind": "organization_group",
                "principal_value": "grp_engineering",
                "access_role": "editor",
            },
        ],
    )
    with engine.begin() as conn:
        assert project_access_service.apply_project_access_intent(conn, intent).outcome == "applied"
        assert (
            project_access_service.get_effective_project_role(
                conn,
                _context("editor", email="viewer@example.com"),
                project["id"],
            )
            == "editor"
        )
        assert (
            project_access_service.get_effective_project_role(
                conn,
                _context("editor", email="guest@elsewhere.net", group_ids=("grp_engineering",)),
                project["id"],
            )
            == "editor"
        )
        assert (
            project_access_service.get_effective_project_role(
                conn,
                _context("editor", email="guest@elsewhere.net"),
                project["id"],
            )
            is None
        )


def test_highest_match_is_capped_by_instance_role(tmp_path) -> None:
    engine, project = _engine_with_project(tmp_path)
    intent = _intent(
        project["id"],
        1,
        bindings=[
            {
                "principal_kind": "email",
                "principal_value": "member@example.com",
                "access_role": "viewer",
            },
            {
                "principal_kind": "email_domain",
                "principal_value": "example.com",
                "access_role": "editor",
            },
        ],
    )
    with engine.begin() as conn:
        project_access_service.apply_project_access_intent(conn, intent)
        assert project_access_service.get_effective_project_role(
            conn, _context("viewer"), project["id"]
        ) == "viewer"
        assert project_access_service.get_effective_project_role(
            conn, _context("editor"), project["id"]
        ) == "editor"


def test_restricted_empty_bindings_is_owner_only(tmp_path) -> None:
    engine, project = _engine_with_project(tmp_path)
    with engine.begin() as conn:
        project_access_service.apply_project_access_intent(
            conn,
            _intent(project["id"], 1),
        )
        assert project_access_service.can_read_project(conn, _context("editor"), project["id"]) is False
        assert project_access_service.can_read_project(conn, trusted_local_context(), project["id"]) is True
        assert project_access_service.can_manage_project(conn, _context("owner"), project["id"]) is True


def test_organization_binding_respects_policy_organization(tmp_path) -> None:
    engine, project = _engine_with_project(tmp_path)
    intent = {
        **_intent(
            project["id"],
            1,
            bindings=[
                {
                    "principal_kind": "organization_group",
                    "principal_value": "grp_engineering",
                    "access_role": "editor",
                }
            ],
        ),
        "organization_id": "org_expected",
    }
    with engine.begin() as conn:
        project_access_service.apply_project_access_intent(conn, intent)
        assert project_access_service.can_chat_project(
            conn,
            _context(
                "editor",
                organization_id="org_expected",
                group_ids=("grp_engineering",),
            ),
            project["id"],
        ) is True
        assert project_access_service.can_read_project(
            conn,
            _context(
                "editor",
                organization_id="org_other",
                group_ids=("grp_engineering",),
            ),
            project["id"],
        ) is False


def test_newer_duplicate_and_stale_intents_are_idempotent(tmp_path) -> None:
    engine, project = _engine_with_project(tmp_path)
    first = _intent(
        project["id"],
        3,
        bindings=[
            {
                "principal_kind": "email",
                "principal_value": "member@example.com",
                "access_role": "viewer",
            }
        ],
    )
    with engine.begin() as conn:
        applied = project_access_service.apply_project_access_intent(conn, first)
        duplicate = project_access_service.apply_project_access_intent(conn, first)
        stale = project_access_service.apply_project_access_intent(
            conn,
            _intent(project["id"], 2),
        )
        policy = project_access_service.get_project_policy(conn, project["id"])

    assert applied.changed is True
    assert duplicate == project_access_service.ProjectAccessIntentResult(
        project_id=project["id"], revision=3, outcome="applied"
    )
    assert stale.outcome == "stale"
    assert policy is not None
    assert policy["policy_revision"] == 1
    assert policy["last_applied_control_plane_revision"] == 3
    assert len(policy["bindings"]) == 1


def test_invalid_or_duplicate_bindings_fail_without_replacing_policy(tmp_path) -> None:
    engine, project = _engine_with_project(tmp_path)
    valid = _intent(
        project["id"],
        1,
        bindings=[
            {
                "principal_kind": "email",
                "principal_value": "member@example.com",
                "access_role": "editor",
            }
        ],
    )
    duplicate = _intent(project["id"], 2, bindings=[valid["bindings"][0], valid["bindings"][0]])
    with engine.begin() as conn:
        project_access_service.apply_project_access_intent(conn, valid)
        rejected = project_access_service.apply_project_access_intent(conn, duplicate)
        policy = project_access_service.get_project_policy(conn, project["id"])

    assert rejected.outcome == "rejected"
    assert rejected.error_code == "duplicate_project_access_principal"
    assert policy is not None
    assert policy["last_applied_control_plane_revision"] == 1
    assert policy["bindings"][0]["access_role"] == "editor"


def test_archived_or_missing_project_intent_is_rejected(tmp_path) -> None:
    engine, project = _engine_with_project(tmp_path)
    with engine.begin() as conn:
        projects_service.archive_project(conn, project["id"])
        result = project_access_service.apply_project_access_intent(
            conn,
            _intent(project["id"], 1),
        )
    assert result.outcome == "rejected"
    assert result.error_code == "project_not_found"


def test_archived_project_denies_non_owner_effective_roles(tmp_path) -> None:
    engine, project = _engine_with_project(tmp_path)
    editor = _context("editor")
    with engine.begin() as conn:
        assert project_access_service.get_effective_project_role(
            conn,
            editor,
            project["id"],
        ) == "editor"
        projects_service.archive_project(conn, project["id"])
        assert project_access_service.get_effective_project_role(
            conn,
            editor,
            project["id"],
        ) is None
        assert project_access_service.can_read_project(
            conn,
            editor,
            project["id"],
        ) is False
        assert project_access_service.get_effective_project_role(
            conn,
            trusted_local_context(),
            project["id"],
        ) == "owner"


def test_policy_delete_cascades_bindings(tmp_path) -> None:
    engine, project = _engine_with_project(tmp_path)
    with engine.begin() as conn:
        project_access_service.apply_project_access_intent(
            conn,
            _intent(
                project["id"],
                1,
                bindings=[
                    {
                        "principal_kind": "email",
                        "principal_value": "member@example.com",
                        "access_role": "editor",
                    }
                ],
            ),
        )
        conn.execute(
            project_access_policies.delete().where(
                project_access_policies.c.project_id == project["id"]
            )
        )
        remaining = conn.execute(
            project_access_bindings.select().where(
                project_access_bindings.c.project_id == project["id"]
            )
        ).all()
    assert remaining == []
