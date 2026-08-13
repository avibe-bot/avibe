"""Route-level coverage for the Skills API's folderless-project handling.

A workbench project whose ``folder_path`` is blank can't hold project-scoped
skills (askill needs a real cwd). ``_resolve_project_dir`` raises
``_ProjectNoFolder`` for it, and every skills route that takes a ``project_id``
must degrade gracefully rather than 500: reads fall back (list → global,
check → empty), the project-independent zip unpack drops the cwd, and the
project-scoped mutations return a clear ``project_no_folder`` error.
"""

from __future__ import annotations

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


def test_list_degrades_to_global_with_flag(folderless, monkeypatch):
    async def fake_list(*, scope, project_dir=None, backends=None):
        assert scope == "global"
        assert project_dir is None
        return {"ok": True, "skills": [{"name": "demo", "scope": "global"}]}

    monkeypatch.setattr(api, "list_skills", fake_list)

    res = app.test_client().get(f"/api/skills?scope=all&project_id={NO_FOLDER}")
    body = res.get_json()

    assert res.status_code == 200
    assert body["ok"] is True
    assert body["project_no_folder"] is True
    assert body["skills"][0]["scope"] == "global"


def test_check_returns_empty(folderless, monkeypatch):
    monkeypatch.setattr(api, "check_skills", _boom)

    res = app.test_client().get(f"/api/skills/check?scope=project&project_id={NO_FOLDER}")

    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "skills": []}


def test_upload_drops_project_cwd(folderless, monkeypatch):
    seen = {}

    async def fake_upload(payload, *, project_dir=None):
        seen["project_dir"] = project_dir
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
    assert seen["project_dir"] is None  # the project cwd was dropped, not errored


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
