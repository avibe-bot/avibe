"""Unit tests for core/services/skills.py — the askill CLI shell.

Hermetic: the subprocess boundary (`_run_askill`) is monkeypatched with canned
``--json`` envelopes, so these run without askill installed and without the
network. They pin the command construction (scope / agent / install flags,
``--skill`` selection, ``check`` / ``update``) and the error paths.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from storage import project_access_service, projects_service, resource_access_service
from storage.db import create_sqlite_engine
from storage.models import metadata

from core.services import skills


def _run(coro):
    return asyncio.run(coro)


class _Recorder:
    """Stand-in for ``_run_askill`` that records args and returns a fixture."""

    def __init__(self, result):
        self.calls: list[dict] = []
        self.result = result

    async def __call__(self, askill_path, args, *, cwd=None, timeout=skills.DEFAULT_TIMEOUT):
        self.calls.append({"path": askill_path, "args": list(args), "cwd": cwd})
        return self.result


class _SequenceRecorder(_Recorder):
    def __init__(self, results):
        super().__init__(None)
        self.results = list(results)

    async def __call__(self, askill_path, args, *, cwd=None, timeout=skills.DEFAULT_TIMEOUT):
        self.calls.append({"path": askill_path, "args": list(args), "cwd": cwd})
        return self.results.pop(0)


def _organization_context(
    subject: str,
    *,
    group_ids: frozenset[str] | None = frozenset({"group-engineering"}),
    role: str = "member",
    instance_role: str = "editor",
) -> resource_access_service.ResourceUserContext:
    return resource_access_service.ResourceUserContext(
        subject=subject,
        email=f"{subject}@example.com",
        organization_id="org-1",
        organization_member_id=f"member-{subject}",
        organization_role=role,
        group_ids=group_ids,
        instance_role=instance_role,
        instance_access_source="organization_group",
        is_remote=True,
    )


def _non_member_context(
    *,
    instance_role: str = "owner",
) -> resource_access_service.ResourceUserContext:
    return resource_access_service.ResourceUserContext(
        subject="legacy-owner",
        email="legacy-owner@example.com",
        instance_role=instance_role,
        instance_access_source="owner",
        is_remote=True,
    )


def _skills_engine(monkeypatch, tmp_path):
    engine = create_sqlite_engine(tmp_path / "skills_acl.sqlite")
    metadata.create_all(engine)
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: engine)
    return engine


def _skill_row(name: str) -> dict:
    return {
        "name": name,
        "scope": "global",
        "path": f"/skills/{name}",
        "agents": [{"id": "codex", "name": "Codex"}],
    }


def _seed_skill_policy(
    conn,
    name: str,
    *,
    access_level: str,
    group_ids: list[str] | None = None,
    backend: str = "codex",
    project_id: str | None = None,
    project_dir: str | None = None,
) -> str:
    resource_id = skills.skill_resource_id(
        backend,
        scope="project" if project_id is not None else "global",
        project_dir=project_dir,
        project_id=project_id,
        name=name,
    )
    resource_access_service.ensure_resource_policy(
        conn,
        resource_kind="skill",
        resource_id=resource_id,
        organization_id="org-1",
        owner_user_id="owner-1",
        access_level=access_level,
        group_ids=group_ids,
    )
    return resource_id


def test_list_global_uses_g_no_cwd(monkeypatch):
    rec = _Recorder({"ok": True, "skills": []})
    monkeypatch.setattr(skills, "_run_askill", rec)
    out = _run(skills.list_skills("askill", scope="global"))
    assert out == {"ok": True, "skills": []}
    assert rec.calls[0]["args"] == ["list", "-g"]
    assert rec.calls[0]["cwd"] is None


def test_mixed_runtime_catalog_does_not_grant_global_skills_to_project_editor(
    monkeypatch,
    tmp_path,
) -> None:
    engine = _skills_engine(monkeypatch, tmp_path)
    monkeypatch.setattr(skills, "_project_role_allows_editor", lambda context, project_id: True)
    try:
        filtered = skills._filter_skill_listing(
            {
                "ok": True,
                "skills": [
                    {"name": "project-skill", "scope": "project"},
                    {"name": "global-skill", "scope": "global", "agents": ["codex"]},
                ],
            },
            scope="all",
            project_dir=str(tmp_path / "project"),
            project_id="project-1",
            backends=["codex"],
            user_context=_organization_context("member-1", instance_role="viewer"),
        )
    finally:
        engine.dispose()

    assert [row["name"] for row in filtered["skills"]] == ["project-skill"]


def test_list_project_uses_p_and_cwd_and_ignores_legacy_backend_filter(monkeypatch):
    rec = _Recorder({"ok": True, "skills": []})
    monkeypatch.setattr(skills, "_run_askill", rec)
    _run(skills.list_skills("askill", scope="project", project_dir="/p", backends=["claude", "codex"]))
    assert rec.calls[0]["args"] == ["list", "-p"]
    assert rec.calls[0]["cwd"] == "/p"


def test_add_global_all(monkeypatch):
    rec = _Recorder({"ok": True, "action": "install"})
    monkeypatch.setattr(skills, "_run_askill", rec)
    _run(skills.add_skill("askill", "gh:o/r", scope="global", backends=["opencode"], all_skills=True))
    assert rec.calls[0]["args"] == ["add", "gh:o/r", "-g", "-a", "claude-code", "opencode", "codex", "--all", "-y"]


def test_add_reports_nothing_installed_when_no_skill_matched(monkeypatch):
    # askill returns ok=True with null results when a @name selector matches
    # nothing (e.g. gh:o/r@does-not-exist); add_skill must surface that as a
    # failure, not a silent success that the UI shows as "installed".
    rec = _Recorder({"ok": True, "action": "install", "results": None, "summary": None, "skills": []})
    monkeypatch.setattr(skills, "_run_askill", rec)
    out = _run(skills.add_skill("askill", "gh:o/r@nope", scope="global"))
    assert out["ok"] is False and out["error"]["code"] == "nothing_installed"


def test_add_succeeds_when_a_skill_was_installed(monkeypatch):
    rec = _Recorder({"ok": True, "action": "install", "summary": {"skills": 1}, "results": [{"skill": "x", "success": True}]})
    monkeypatch.setattr(skills, "_run_askill", rec)
    out = _run(skills.add_skill("askill", "gh:o/r@x", scope="global"))
    assert out["ok"] is True
    assert rec.calls[0]["cwd"] is None


def test_add_multi_backend_uses_single_a(monkeypatch):
    # askill -a is variadic and each later -a REPLACES the prior values, so all
    # selected agents must share one -a, else only the last backend installs.
    rec = _Recorder({"ok": True, "action": "install"})
    monkeypatch.setattr(skills, "_run_askill", rec)
    _run(skills.add_skill("askill", "gh:o/r", scope="global", backends=["claude", "opencode", "codex"], all_skills=True))
    assert rec.calls[0]["args"] == ["add", "gh:o/r", "-g", "-a", "claude-code", "opencode", "codex", "--all", "-y"]


def test_add_project_has_no_p_flag_and_uses_cwd(monkeypatch):
    # add/remove do NOT take -p — project scope is the default, selected by cwd.
    rec = _Recorder({"ok": True, "action": "install"})
    monkeypatch.setattr(skills, "_run_askill", rec)
    _run(skills.add_skill("askill", "./pkg", scope="project", project_dir="/p", copy=True))
    assert rec.calls[0]["args"] == ["add", "./pkg", "-a", "claude-code", "opencode", "codex", "--copy", "-y"]
    assert rec.calls[0]["cwd"] == "/p"


def test_add_with_skill_selector(monkeypatch):
    rec = _Recorder({"ok": True, "action": "install"})
    monkeypatch.setattr(skills, "_run_askill", rec)
    _run(skills.add_skill("askill", "./pkg", scope="project", project_dir="/p", skill="formatter", backends=["opencode"]))
    assert rec.calls[0]["args"] == ["add", "./pkg", "-a", "claude-code", "opencode", "codex", "--skill", "formatter", "-y"]


def test_preview_uses_list_flag(monkeypatch):
    rec = _Recorder({"ok": True, "action": "preview", "skills": []})
    monkeypatch.setattr(skills, "_run_askill", rec)
    _run(skills.preview_source("askill", "gh:o/r", project_dir="/p"))
    assert rec.calls[0]["args"] == ["add", "gh:o/r", "--list"]
    assert rec.calls[0]["cwd"] == "/p"


def test_remove_project_no_p_flag(monkeypatch):
    rec = _Recorder({"ok": True})
    monkeypatch.setattr(skills, "_run_askill", rec)
    _run(skills.remove_skill("askill", "pdf-tools", scope="project", project_dir="/p", backends=["claude"]))
    assert rec.calls[0]["args"] == ["remove", "pdf-tools", "-a", "claude-code", "opencode", "codex"]
    assert rec.calls[0]["cwd"] == "/p"


def test_remove_global(monkeypatch):
    rec = _Recorder({"ok": True})
    monkeypatch.setattr(skills, "_run_askill", rec)
    _run(skills.remove_skill("askill", "pdf-tools", scope="global"))
    assert rec.calls[0]["args"] == ["remove", "pdf-tools", "-g", "-a", "claude-code", "opencode", "codex"]
    assert rec.calls[0]["cwd"] is None


def test_find_passes_query(monkeypatch):
    rec = _Recorder({"ok": True, "skills": [{"name": "memory"}]})
    monkeypatch.setattr(skills, "_run_askill", rec)
    out = _run(skills.find_skills("askill", "memory"))
    assert rec.calls[0]["args"] == ["find", "memory"]
    assert out["skills"][0]["name"] == "memory"


def test_check_global_and_project(monkeypatch):
    rec = _Recorder({"ok": True, "summary": {}, "skills": []})
    monkeypatch.setattr(skills, "_run_askill", rec)
    _run(skills.check("askill", scope="global"))
    assert rec.calls[0]["args"] == ["check", "-g"]
    assert rec.calls[0]["cwd"] is None
    _run(skills.check("askill", scope="project", project_dir="/p"))
    assert rec.calls[1]["args"] == ["check"]
    assert rec.calls[1]["cwd"] == "/p"


def test_update_one_skill(monkeypatch):
    rec = _Recorder({"ok": True, "results": []})
    monkeypatch.setattr(skills, "_run_askill", rec)
    _run(skills.update("askill", "pdf-tools", scope="project", project_dir="/p"))
    assert rec.calls[0]["args"] == ["update", "pdf-tools", "-y"]
    assert rec.calls[0]["cwd"] == "/p"
    _run(skills.update("askill", "pdf-tools", scope="global"))
    assert rec.calls[1]["args"] == ["update", "pdf-tools", "-g", "-y"]


def test_skill_resource_id_is_stable_and_backend_scoped(tmp_path) -> None:
    global_id = skills.skill_resource_id("codex", scope="global", project_dir=None, name="Release Tools")
    legacy_project_id = skills.skill_resource_id(
        "codex",
        scope="project",
        project_dir=str(tmp_path / "project"),
        name="Release Tools",
    )
    stable_project_id = skills.skill_resource_id(
        "codex",
        scope="project",
        project_dir=str(tmp_path / "project"),
        project_id="proj_123abc",
        name="Release Tools",
    )
    moved_project_id = skills.skill_resource_id(
        "codex",
        scope="project",
        project_dir=str(tmp_path / "project-renamed"),
        project_id="proj_123abc",
        name="Release Tools",
    )

    assert global_id == "codex:global:global:release-tools"
    assert legacy_project_id.startswith("codex:project:project-")
    assert legacy_project_id.endswith(":release-tools")
    assert stable_project_id == moved_project_id
    assert stable_project_id.startswith("codex:project:project-proj_123abc")
    assert stable_project_id.endswith(":release-tools")
    assert stable_project_id != legacy_project_id


def test_active_org_members_list_all_skills_without_resource_acl_filtering(monkeypatch, tmp_path) -> None:
    engine = _skills_engine(monkeypatch, tmp_path)
    try:
        with engine.begin() as connection:
            _seed_skill_policy(connection, "private-skill", access_level="private")
            _seed_skill_policy(connection, "public-skill", access_level="public")
            _seed_skill_policy(
                connection,
                "scoped-skill",
                access_level="scope",
                group_ids=["group-engineering"],
            )

        rec = _Recorder(
            {
                "ok": True,
                "summary": {"global": 3, "project": 0},
                "skills": [_skill_row("private-skill"), _skill_row("public-skill"), _skill_row("scoped-skill")],
            }
        )
        monkeypatch.setattr(skills, "_run_askill", rec)

        owner = _run(skills.list_skills("askill", scope="global", user_context=_organization_context("owner-1")))
        member = _run(skills.list_skills("askill", scope="global", user_context=_organization_context("member-1")))
        missing_groups = _run(
            skills.list_skills("askill", scope="global", user_context=_organization_context("member-2", group_ids=None))
        )
    finally:
        engine.dispose()

    expected_names = ["private-skill", "public-skill", "scoped-skill"]
    assert [skill["name"] for skill in owner["skills"]] == expected_names
    assert [skill["name"] for skill in member["skills"]] == expected_names
    assert [skill["name"] for skill in missing_groups["skills"]] == expected_names
    assert missing_groups["summary"] == {"global": 3, "project": 0}


def test_active_org_members_check_all_skills_without_resource_acl_filtering(monkeypatch, tmp_path) -> None:
    engine = _skills_engine(monkeypatch, tmp_path)
    try:
        with engine.begin() as connection:
            _seed_skill_policy(connection, "private-skill", access_level="private")
            _seed_skill_policy(connection, "public-skill", access_level="public")
            _seed_skill_policy(
                connection,
                "scoped-skill",
                access_level="scope",
                group_ids=["group-engineering"],
            )
        rec = _Recorder(
            {
                "ok": True,
                "summary": {"total": 3, "updateAvailable": 2, "upToDate": 1, "uncheckable": 0},
                "skills": [
                    {"name": "private-skill", "scope": "global", "status": "update_available"},
                    {"name": "public-skill", "scope": "global", "status": "up_to_date"},
                    {"name": "scoped-skill", "scope": "global", "status": "update_available"},
                ],
            }
        )
        monkeypatch.setattr(skills, "_run_askill", rec)

        member = _run(skills.check("askill", scope="global", user_context=_organization_context("member-1")))
        missing_groups = _run(
            skills.check(
                "askill",
                scope="global",
                user_context=_organization_context("member-2", group_ids=None),
            )
        )
    finally:
        engine.dispose()

    expected_names = ["private-skill", "public-skill", "scoped-skill"]
    expected_summary = {"total": 3, "updateAvailable": 2, "upToDate": 1, "uncheckable": 0}
    assert [skill["name"] for skill in member["skills"]] == expected_names
    assert member["summary"] == expected_summary
    assert [skill["name"] for skill in missing_groups["skills"]] == expected_names
    assert missing_groups["summary"] == expected_summary


def test_active_org_members_list_project_skills_without_project_acl(monkeypatch, tmp_path) -> None:
    engine = _skills_engine(monkeypatch, tmp_path)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    listing = {
        "ok": True,
        "skills": [
            {
                **_skill_row("project-skill"),
                "scope": "project",
                "path": str(project_dir / ".agents" / "skills" / "project-skill"),
            }
        ],
    }
    try:
        with engine.begin() as connection:
            project = projects_service.create_project(connection, str(project_dir))
            result = project_access_service.apply_project_access_intent(
                connection,
                {
                    "project_id": project["id"],
                    "revision": 1,
                    "mode": "restricted",
                    "organization_id": "org-1",
                    "bindings": [
                        {
                            "principal_kind": "organization_group",
                            "principal_value": "group-engineering",
                            "access_role": "editor",
                        }
                    ],
                },
            )
            assert result.outcome == "applied"
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind="skill",
                resource_id=skills.skill_resource_id(
                    "codex",
                    scope="project",
                    project_dir=str(project_dir),
                    project_id=project["id"],
                    name="project-skill",
                ),
                organization_id="org-1",
                owner_user_id="owner-1",
                access_level="scope",
                group_ids=["group-engineering"],
            )

        allowed_recorder = _Recorder(listing)
        monkeypatch.setattr(skills, "_run_askill", allowed_recorder)
        allowed = _run(
            skills.list_skills(
                "askill",
                scope="project",
                project_dir=str(project_dir),
                project_id=project["id"],
                user_context=_organization_context("member-1"),
            )
        )
        assert [item["name"] for item in allowed["skills"]] == ["project-skill"]

        allowed_all_recorder = _Recorder(listing)
        monkeypatch.setattr(skills, "_run_askill", allowed_all_recorder)
        allowed_all = _run(
            skills.list_skills(
                "askill",
                scope="all",
                project_dir=str(project_dir),
                project_id=project["id"],
                user_context=_organization_context("member-1"),
            )
        )
        assert [item["name"] for item in allowed_all["skills"]] == ["project-skill"]

        unmatched_group_recorder = _Recorder(listing)
        monkeypatch.setattr(skills, "_run_askill", unmatched_group_recorder)
        unmatched_group = _run(
            skills.list_skills(
                "askill",
                scope="project",
                project_dir=str(project_dir),
                project_id=project["id"],
                user_context=_organization_context(
                    "member-2",
                    group_ids=frozenset({"group-sales"}),
                ),
            )
        )
        assert [item["name"] for item in unmatched_group["skills"]] == ["project-skill"]
        assert [call["args"] for call in unmatched_group_recorder.calls] == [["list", "-p"]]

        unmatched_group_all_recorder = _Recorder(listing)
        monkeypatch.setattr(skills, "_run_askill", unmatched_group_all_recorder)
        unmatched_group_all = _run(
            skills.list_skills(
                "askill",
                scope="all",
                project_dir=str(project_dir),
                project_id=project["id"],
                user_context=_organization_context(
                    "member-2",
                    group_ids=frozenset({"group-sales"}),
                ),
            )
        )
        assert [item["name"] for item in unmatched_group_all["skills"]] == ["project-skill"]
        assert [call["args"] for call in unmatched_group_all_recorder.calls] == [["list"]]
    finally:
        engine.dispose()


def test_effective_project_viewer_gets_safe_skill_payload_and_cannot_mutate(monkeypatch, tmp_path) -> None:
    engine = _skills_engine(monkeypatch, tmp_path)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    listing = {
        "ok": True,
        "summary": {"total": 3, "updateAvailable": 2, "upToDate": 1, "uncheckable": 0},
        "skills": [
            {
                **_skill_row("global-skill"),
                "path": "/global/skills/global-skill",
            },
            {
                **_skill_row("project-skill"),
                "scope": "project",
                "path": str(project_dir / ".agents" / "skills" / "project-skill"),
                "status": "update_available",
            }
        ],
    }
    try:
        with engine.begin() as connection:
            project = projects_service.create_project(connection, str(project_dir))
            result = project_access_service.apply_project_access_intent(
                connection,
                {
                    "project_id": project["id"],
                    "revision": 1,
                    "mode": "restricted",
                    "organization_id": "org-1",
                    "bindings": [
                        {
                            "principal_kind": "email",
                            "principal_value": "member-1@example.com",
                            "access_role": "viewer",
                        }
                    ],
                },
            )
            assert result.outcome == "applied"
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind="skill",
                resource_id=skills.skill_resource_id(
                    "codex",
                    scope="project",
                    project_dir=str(project_dir),
                    project_id=project["id"],
                    name="project-skill",
                ),
                organization_id="org-1",
                owner_user_id="owner-1",
                access_level="public",
            )

        recorder = _Recorder(listing)
        monkeypatch.setattr(skills, "_run_askill", recorder)
        viewer = _organization_context("member-1")
        safe = _run(
            skills.list_skills(
                "askill",
                scope="project",
                project_dir=str(project_dir),
                project_id=project["id"],
                user_context=viewer,
            )
        )
        assert safe["skills"][0]["path"] == "/global/skills/global-skill"
        assert safe["skills"][1]["path"] == ""
        checked = _run(
            skills.check(
                "askill",
                scope="project",
                project_dir=str(project_dir),
                project_id=project["id"],
                user_context=viewer,
            )
        )
        assert checked["summary"] == {"total": 2, "updateAvailable": 1, "upToDate": 0, "uncheckable": 0}

        with pytest.raises(skills.SkillAccessError):
            _run(
                skills.add_skill(
                    "askill",
                    "gh:owner/repo",
                    scope="project",
                    project_dir=str(project_dir),
                    project_id=project["id"],
                    user_context=viewer,
                )
            )
        with pytest.raises(skills.SkillAccessError):
            _run(
                skills.preview_source(
                    "askill",
                    ".",
                    project_dir=str(project_dir),
                    project_id=project["id"],
                    user_context=viewer,
                )
            )
        assert len(recorder.calls) == 2
    finally:
        engine.dispose()


def test_active_org_skill_listing_returns_complete_runtime_payload(monkeypatch, tmp_path) -> None:
    engine = _skills_engine(monkeypatch, tmp_path)
    raw_skill = {
        **_skill_row("public-skill"),
        "description": "safe description",
        "sourceUrl": "file:///Users/alex/private-skill",
        "installSource": "/Users/alex/private-skill",
        "unknownLocalField": "secret",
        "agents": [
            {"id": "codex", "name": "Codex", "path": "/Users/alex/.codex"}
        ],
    }
    try:
        with engine.begin() as connection:
            _seed_skill_policy(connection, "public-skill", access_level="public")
        recorder = _Recorder(
            {
                "ok": True,
                "skills": [raw_skill],
            }
        )
        monkeypatch.setattr(skills, "_run_askill", recorder)

        result = _run(
            skills.list_skills(
                "askill",
                scope="global",
                user_context=_organization_context("member-1"),
            )
        )
    finally:
        engine.dispose()

    assert result["skills"] == [raw_skill]


def test_active_org_members_mutate_skills_without_owner_preflight(monkeypatch, tmp_path) -> None:
    engine = _skills_engine(monkeypatch, tmp_path)
    try:
        with engine.begin() as connection:
            _seed_skill_policy(connection, "private-skill", access_level="private")

        member_recorder = _Recorder({"ok": True})
        monkeypatch.setattr(skills, "_run_askill", member_recorder)
        assert _run(
            skills.remove_skill(
                "askill",
                "private-skill",
                scope="global",
                user_context=_organization_context("member-1"),
            )
        ) == {"ok": True}
        assert [call["args"] for call in member_recorder.calls] == [
            ["remove", "private-skill", "-g", "-a", "claude-code", "opencode", "codex"]
        ]
        with engine.connect() as connection:
            assert resource_access_service.get_resource_policy(
                "skill",
                skills.skill_resource_id(
                    "codex",
                    scope="global",
                    project_dir=None,
                    name="private-skill",
                ),
                connection=connection,
            ) is None

        with engine.begin() as connection:
            _seed_skill_policy(connection, "private-skill", access_level="private")

        admin_editor_recorder = _Recorder({"ok": True})
        monkeypatch.setattr(skills, "_run_askill", admin_editor_recorder)
        assert _run(
            skills.update(
                "askill",
                "private-skill",
                scope="global",
                user_context=_organization_context("member-2", role="admin"),
            )
        ) == {"ok": True}
        assert [call["args"] for call in admin_editor_recorder.calls] == [
            ["update", "private-skill", "-g", "-y"]
        ]

        add_result = {
            "ok": True,
            "action": "install",
            "summary": {"skills": 1},
            "selectedAgents": ["codex"],
            "results": [{"skill": "private-skill", "success": True}],
        }
        add_recorder = _Recorder(add_result)
        monkeypatch.setattr(skills, "_run_askill", add_recorder)
        assert _run(
            skills.add_skill(
                "askill",
                "gh:owner/repo",
                scope="global",
                skill="private-skill",
                backends=["codex"],
                user_context=_organization_context("member-1"),
            )
        ) == add_result
        assert [call["args"] for call in add_recorder.calls] == [
            [
                "add",
                "gh:owner/repo",
                "-g",
                "-a",
                "claude-code",
                "opencode",
                "codex",
                "--skill",
                "private-skill",
                "-y",
            ]
        ]
    finally:
        engine.dispose()


def test_remove_skill_deletes_all_legacy_backend_policies(monkeypatch, tmp_path) -> None:
    engine = _skills_engine(monkeypatch, tmp_path)
    try:
        with engine.begin() as connection:
            codex_id = _seed_skill_policy(connection, "shared-skill", access_level="private")
            claude_id = _seed_skill_policy(
                connection,
                "shared-skill",
                access_level="private",
                backend="claude",
            )
        recorder = _Recorder(
            {
                "ok": True,
                "selectedAgents": ["codex", "claude"],
                "removedAgents": ["codex"],
            }
        )
        monkeypatch.setattr(skills, "_run_askill", recorder)

        result = _run(
            skills.remove_skill(
                "askill",
                "shared-skill",
                scope="global",
                backends=["codex", "claude"],
                user_context=_organization_context("member-1"),
            )
        )
        with engine.connect() as connection:
            codex_policy = resource_access_service.get_resource_policy("skill", codex_id, connection=connection)
            claude_policy = resource_access_service.get_resource_policy("skill", claude_id, connection=connection)
    finally:
        engine.dispose()

    assert result["ok"] is True
    assert [call["args"] for call in recorder.calls] == [
        ["remove", "shared-skill", "-g", "-a", "claude-code", "opencode", "codex"]
    ]
    assert codex_policy is None
    assert claude_policy is None


@pytest.mark.parametrize("operation", ["add", "remove", "update"])
def test_viewer_skill_mutations_fail_before_cli(
    monkeypatch,
    tmp_path,
    operation: str,
) -> None:
    engine = _skills_engine(monkeypatch, tmp_path)
    recorder = _Recorder({"ok": True})
    monkeypatch.setattr(skills, "_run_askill", recorder)
    try:
        if operation == "add":
            mutation = skills.add_skill(
                "askill",
                "gh:owner/repo",
                scope="global",
                skill="missing-skill",
                user_context=_non_member_context(instance_role="viewer"),
            )
        elif operation == "remove":
            mutation = skills.remove_skill(
                "askill",
                "missing-skill",
                scope="global",
                user_context=_non_member_context(instance_role="viewer"),
            )
        else:
            mutation = skills.update(
                "askill",
                "missing-skill",
                scope="global",
                user_context=_non_member_context(instance_role="viewer"),
            )
        with pytest.raises(skills.SkillAccessError):
            _run(mutation)
    finally:
        engine.dispose()

    assert recorder.calls == []


def test_remote_skill_add_registers_private_policy(monkeypatch, tmp_path) -> None:
    engine = _skills_engine(monkeypatch, tmp_path)
    try:
        rec = _SequenceRecorder(
            [
                {"ok": True, "skills": []},
                {
                    "ok": True,
                    "action": "install",
                    "summary": {"skills": 1},
                    "selectedAgents": ["codex"],
                    "results": [{"skill": "new-skill", "success": True}],
                },
            ]
        )
        monkeypatch.setattr(skills, "_run_askill", rec)

        result = _run(
            skills.add_skill(
                "askill",
                "gh:owner/repo",
                scope="global",
                skill="new-skill",
                backends=["codex"],
                user_context=_organization_context("member-1", instance_role="owner"),
            )
        )
        with engine.connect() as connection:
            policy = resource_access_service.get_resource_policy(
                "skill",
                skills.skill_resource_id("codex", scope="global", project_dir=None, name="new-skill"),
                connection=connection,
            )
    finally:
        engine.dispose()

    assert result["ok"] is True
    assert policy is not None
    assert policy["owner_user_id"] == "member-1"
    assert policy["access_level"] == "private"


def test_active_org_member_can_replace_installed_legacy_skill(monkeypatch, tmp_path) -> None:
    engine = _skills_engine(monkeypatch, tmp_path)
    install_result = {
        "ok": True,
        "action": "install",
        "summary": {"skills": 1},
        "selectedAgents": ["codex"],
        "results": [{"skill": "legacy-skill", "success": True}],
    }
    member_recorder = _Recorder(install_result)
    monkeypatch.setattr(skills, "_run_askill", member_recorder)
    try:
        result = _run(
            skills.add_skill(
                "askill",
                "gh:owner/repo",
                scope="global",
                skill="legacy-skill",
                backends=["codex"],
                user_context=_organization_context("member-1"),
            )
        )
        with engine.connect() as connection:
            policy = resource_access_service.get_resource_policy(
                "skill",
                skills.skill_resource_id(
                    "codex",
                    scope="global",
                    project_dir=None,
                    name="legacy-skill",
                ),
                connection=connection,
            )
    finally:
        engine.dispose()

    assert [call["args"] for call in member_recorder.calls] == [
        ["add", "gh:owner/repo", "-g", "-a", "claude-code", "opencode", "codex", "--skill", "legacy-skill", "-y"],
    ]
    assert result == install_result
    assert policy is not None
    assert policy["owner_user_id"] == "member-1"


def test_instance_owner_skill_add_does_not_require_organization_membership(
    monkeypatch,
    tmp_path,
) -> None:
    engine = _skills_engine(monkeypatch, tmp_path)
    install_result = {
        "ok": True,
        "action": "install",
        "summary": {"skills": 1},
        "selectedAgents": ["codex"],
        "results": [{"skill": "legacy-skill", "success": True}],
    }
    recorder = _Recorder(install_result)
    monkeypatch.setattr(skills, "_run_askill", recorder)
    try:
        result = _run(
            skills.add_skill(
                "askill",
                "gh:owner/repo",
                scope="global",
                skill="legacy-skill",
                backends=["codex"],
                user_context=_non_member_context(),
            )
        )
    finally:
        engine.dispose()

    assert result == install_result
    assert [call["args"] for call in recorder.calls] == [
        ["add", "gh:owner/repo", "-g", "-a", "claude-code", "opencode", "codex", "--skill", "legacy-skill", "-y"]
    ]


def test_active_org_editor_can_upload_skill_zip(monkeypatch, tmp_path) -> None:
    import base64
    import io
    import tempfile
    import zipfile
    from pathlib import Path

    from vibe import api

    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("uploaded-skill/SKILL.md", "# Uploaded Skill\n")

    async def preview_uploaded_skill(_callback, **_kwargs):
        return {"ok": True, "skills": [{"name": "uploaded-skill"}]}

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(api, "_skills_guarded", preview_uploaded_skill)

    result = _run(
        api.upload_skill_zip(
            {
                "content_base64": base64.b64encode(archive_bytes.getvalue()).decode(
                    "ascii"
                )
            },
            user_context=_organization_context("member-1"),
        )
    )

    unpack_dir = Path(result["dir"])
    assert result["ok"] is True
    assert result["skills"] == [{"name": "uploaded-skill"}]
    assert unpack_dir.parent.parent == tmp_path
    assert (unpack_dir / "uploaded-skill" / "SKILL.md").read_text() == "# Uploaded Skill\n"


def test_viewer_cannot_upload_skill_zip(monkeypatch) -> None:
    from vibe import api
    from vibe.authorization import InstanceAuthorizationError

    async def unexpected_preview(_callback, **_kwargs):
        raise AssertionError("viewer upload must fail before inspecting the archive")

    monkeypatch.setattr(api, "_skills_guarded", unexpected_preview)

    with pytest.raises(InstanceAuthorizationError):
        _run(
            api.upload_skill_zip(
                {"content_base64": "not-an-archive"},
                user_context=_non_member_context(instance_role="viewer"),
            )
        )


def test_invalid_backend_raises(monkeypatch):
    monkeypatch.setattr(skills, "_run_askill", _Recorder({"ok": True}))
    with pytest.raises(skills.SkillsError) as info:
        _run(skills.list_skills("askill", scope="all", backends=["bogus"]))
    assert info.value.code == "invalid_backend"


def test_invalid_scope_raises(monkeypatch):
    monkeypatch.setattr(skills, "_run_askill", _Recorder({"ok": True}))
    with pytest.raises(skills.SkillsError) as info:
        _run(skills.add_skill("askill", "gh:o/r", scope="all"))
    assert info.value.code == "invalid_scope"


def test_project_scope_requires_project_dir(monkeypatch):
    # A project-scoped op without a project dir must not fall back to the
    # server's cwd — it raises so the route returns an error instead.
    monkeypatch.setattr(skills, "_run_askill", _Recorder({"ok": True}))
    for call in (
        lambda: skills.add_skill("askill", "gh:o/r", scope="project"),
        lambda: skills.remove_skill("askill", "x", scope="project"),
        lambda: skills.check("askill", scope="project"),
        lambda: skills.update("askill", "x", scope="project"),
    ):
        with pytest.raises(skills.SkillsError) as info:
            _run(call())
        assert info.value.code == "project_required"


def test_subprocess_env_prepends_binary_dir(monkeypatch):
    # askill is a Node CLI; its bin dir (where node lives) must lead PATH.
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    env = skills._subprocess_env("/opt/nvm/v20/bin/askill")
    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == "/opt/nvm/v20/bin"
    assert "/usr/bin" in parts


def test_missing_binary_raises_lookup():
    with pytest.raises(LookupError):
        _run(skills._run_askill("", ["list"]))


def test_missing_project_dir_error_preserves_host_path_for_authorized_service_call(tmp_path):
    missing = tmp_path / "deleted-project"
    with pytest.raises(skills.SkillsError) as info:
        _run(skills._run_askill("askill", ["list"], cwd=str(missing)))
    assert info.value.code == "project_dir_missing"
    assert str(missing) in info.value.message
