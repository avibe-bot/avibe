"""Route-level coverage for the Skills API's folderless-project handling.

A workbench project whose ``folder_path`` is blank can't hold project-scoped
skills (askill needs a real cwd). ``_resolve_project_dir`` raises
``_ProjectNoFolder`` for it, and every skills route that takes a ``project_id``
must degrade gracefully rather than 500: reads fall back (list → global,
check → empty), the project-independent zip unpack drops the cwd, and the
project-scoped mutations return a clear ``project_no_folder`` error.
"""

from __future__ import annotations

import asyncio

import pytest

from vibe import api, ui_server
from vibe.ui_server import app

from tests.ui_server_test_helpers import csrf_headers

NO_FOLDER = "proj_nofolder"


@pytest.fixture
def folderless(monkeypatch):
    def fake_resolve(project_id):
        if project_id == NO_FOLDER:
            raise ui_server._ProjectNoFolder(project_id)
        return None

    monkeypatch.setattr(ui_server, "_resolve_project_dir", fake_resolve)
    return monkeypatch


def _boom(*_args, **_kwargs):
    raise AssertionError("askill must not be reached for a folderless project")


def test_skills_guarded_redacts_missing_project_path(monkeypatch, tmp_path):
    from core.services import skills as skills_service
    from storage import project_access_service
    from vibe.authorization import AuthorizationContext

    monkeypatch.setattr(api, "resolve_cli_path", lambda _name: "askill")

    async def fail(_askill, _service):
        raise skills_service.SkillsError(
            "project_dir_missing",
            f"project folder not found: {tmp_path / 'deleted-project'}",
        )

    class _Engine:
        class _Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_exc_info):
                return False

        def connect(self):
            return self._Connection()

    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: _Engine())
    monkeypatch.setattr(project_access_service, "get_effective_project_role", lambda *_args: "viewer")
    context = AuthorizationContext(instance_role="editor", subject="viewer", is_remote=True)
    result = asyncio.run(
        api._skills_guarded(fail, user_context=context, project_id="proj-viewer")
    )

    assert result["error"]["code"] == "project_dir_missing"
    assert result["error"]["message"] == "The configured project folder is unavailable."
    assert str(tmp_path) not in result["error"]["message"]


def test_skills_guarded_preserves_missing_project_path_for_effective_editor(monkeypatch, tmp_path):
    from core.services import skills as skills_service
    from storage import project_access_service
    from vibe.authorization import AuthorizationContext

    monkeypatch.setattr(api, "resolve_cli_path", lambda _name: "askill")

    async def fail(_askill, _service):
        raise skills_service.SkillsError(
            "project_dir_missing",
            f"project folder not found: {tmp_path / 'deleted-project'}",
        )

    class _Engine:
        class _Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_exc_info):
                return False

        def connect(self):
            return self._Connection()

    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: _Engine())
    monkeypatch.setattr(project_access_service, "get_effective_project_role", lambda *_args: "editor")
    context = AuthorizationContext(instance_role="editor", subject="editor", is_remote=True)
    result = asyncio.run(
        api._skills_guarded(fail, user_context=context, project_id="proj-editor")
    )

    assert str(tmp_path) in result["error"]["message"]


def test_list_degrades_to_global_with_flag(folderless, monkeypatch):
    async def fake_list(*, scope, project_dir=None, backends=None, user_context=None):
        assert scope == "global"
        assert project_dir is None
        assert user_context.is_instance_owner
        return {"ok": True, "skills": [{"name": "demo", "scope": "global"}]}

    monkeypatch.setattr(api, "list_skills", fake_list)

    res = app.test_client().get(f"/api/skills?scope=all&project_id={NO_FOLDER}")
    body = res.get_json()

    assert res.status_code == 200
    assert body["ok"] is True
    assert body["project_no_folder"] is True
    assert body["skills"][0]["scope"] == "global"


def test_resolve_project_dir_uses_request_authorization_context(monkeypatch):
    from vibe.authorization import AuthorizationContext
    from vibe.ui_compat import g

    seen = {}

    class _Engine:
        class _Connection:
            def __enter__(self):
                return object()

            def __exit__(self, *_exc_info):
                return False

        def connect(self):
            return self._Connection()

    def fake_get_project_workdir(conn, project_id, *, authorization_context=None):
        seen["project_id"] = project_id
        seen["authorization_context"] = authorization_context
        return "/tmp/project"

    monkeypatch.setattr(ui_server, "_projects_engine", lambda: _Engine())
    monkeypatch.setattr("storage.projects_service.get_project_workdir", fake_get_project_workdir)
    remote_context = AuthorizationContext(instance_role="editor", is_remote=True, subject="user-1")

    with app.test_request_context("/api/skills?project_id=proj-restricted"):
        g.authorization_context = remote_context
        assert ui_server._resolve_project_dir("proj-restricted") == "/tmp/project"

    assert seen == {
        "project_id": "proj-restricted",
        "authorization_context": remote_context,
    }


def test_check_returns_empty(folderless, monkeypatch):
    monkeypatch.setattr(api, "check_skills", _boom)

    res = app.test_client().get(f"/api/skills/check?scope=project&project_id={NO_FOLDER}")

    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "skills": []}


def test_upload_drops_project_cwd(folderless, monkeypatch):
    seen = {}

    async def fake_upload(payload, *, project_dir=None, user_context=None):
        seen["project_dir"] = project_dir
        seen["is_instance_owner"] = user_context.is_instance_owner
        return {"ok": True, "skills": [], "dir": "/tmp/askill-upload-x"}

    monkeypatch.setattr(api, "upload_skill_zip", fake_upload)

    client = app.test_client()
    res = client.post(
        "/api/skills/upload",
        json={"content_base64": "", "project_id": NO_FOLDER},
        headers=csrf_headers(client),
    )

    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    assert seen == {
        "project_dir": None,
        "is_instance_owner": True,
    }


def test_list_preserves_project_id_for_real_project(monkeypatch):
    async def fake_list(
        *,
        scope,
        project_dir=None,
        backends=None,
        project_id=None,
        user_context=None,
    ):
        assert scope == "project"
        assert project_dir == "/tmp/project"
        assert project_id == "proj-real"
        assert user_context.is_instance_owner
        return {"ok": True, "skills": [{"name": "demo", "scope": "project"}]}

    def fake_resolve(project_id):
        if project_id == "proj-real":
            return "/tmp/project"
        raise LookupError(project_id)

    monkeypatch.setattr(ui_server, "_resolve_project_dir", fake_resolve)
    monkeypatch.setattr(api, "list_skills", fake_list)

    res = app.test_client().get("/api/skills?scope=project&project_id=proj-real")
    body = res.get_json()

    assert res.status_code == 200
    assert body["ok"] is True
    assert body["skills"][0]["scope"] == "project"


def test_upload_preserves_project_id_for_real_project(monkeypatch):
    seen = {}

    async def fake_upload(
        payload,
        *,
        project_dir=None,
        project_id=None,
        user_context=None,
    ):
        seen["project_dir"] = project_dir
        seen["project_id"] = project_id
        seen["is_instance_owner"] = user_context.is_instance_owner
        return {"ok": True, "skills": [], "dir": "/tmp/askill-upload-x"}

    def fake_resolve(project_id):
        if project_id == "proj-real":
            return "/tmp/project"
        raise LookupError(project_id)

    monkeypatch.setattr(ui_server, "_resolve_project_dir", fake_resolve)
    monkeypatch.setattr(api, "upload_skill_zip", fake_upload)

    client = app.test_client()
    res = client.post(
        "/api/skills/upload",
        json={"content_base64": "", "project_id": "proj-real"},
        headers=csrf_headers(client),
    )

    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    assert seen == {
        "project_dir": "/tmp/project",
        "project_id": "proj-real",
        "is_instance_owner": True,
    }


def test_project_viewer_cannot_reach_skill_mutations(monkeypatch):
    denied = ui_server._coded_error_response(
        "resource_access_forbidden",
        "Skill access is not permitted.",
        403,
    )
    monkeypatch.setattr(
        ui_server,
        "_require_project_editor_for_skill_mutation",
        lambda project_id, **_kwargs: denied if project_id == "proj-viewer" else None,
    )
    monkeypatch.setattr(ui_server, "_resolve_project_dir", lambda _project_id: "/tmp/project")
    monkeypatch.setattr(api, "add_skill", _boom)

    client = app.test_client()
    response = client.post(
        "/api/skills",
        json={"project_id": "proj-viewer", "source": "gh:owner/repo"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "resource_access_forbidden"


@pytest.mark.parametrize(
    "method,path,attr,payload",
    [
        ("post", "/api/skills", "add_skill", {"source": "gh:owner/repo", "scope": "project"}),
        ("delete", "/api/skills/demo?scope=project", "remove_skill", None),
        ("post", "/api/skills/update", "update_skill", {"name": "demo", "scope": "project"}),
    ],
)
def test_mutations_return_clear_error(folderless, monkeypatch, method, path, attr, payload):
    monkeypatch.setattr(api, attr, _boom)

    client = app.test_client()
    headers = csrf_headers(client)
    if method == "delete":
        sep = "&" if "?" in path else "?"
        res = client.delete(f"{path}{sep}project_id={NO_FOLDER}", headers=headers)
    else:
        res = client.post(path, json={**(payload or {}), "project_id": NO_FOLDER}, headers=headers)

    assert res.status_code == 400
    assert res.get_json()["error"]["code"] == "project_no_folder"
