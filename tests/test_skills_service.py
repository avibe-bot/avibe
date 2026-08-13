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


def test_list_global_uses_g_no_cwd(monkeypatch):
    rec = _Recorder({"ok": True, "skills": []})
    monkeypatch.setattr(skills, "_run_askill", rec)
    out = _run(skills.list_skills("askill", scope="global"))
    assert out == {"ok": True, "skills": []}
    assert rec.calls[0]["args"] == ["list", "-g"]
    assert rec.calls[0]["cwd"] is None


def test_list_project_uses_p_and_cwd_and_agents(monkeypatch):
    rec = _Recorder({"ok": True, "skills": []})
    monkeypatch.setattr(skills, "_run_askill", rec)
    _run(skills.list_skills("askill", scope="project", project_dir="/p", backends=["claude", "codex"]))
    # list supports -p; agents expand to askill ids under ONE variadic -a.
    assert rec.calls[0]["args"] == ["list", "-p", "-a", "claude-code", "codex"]
    assert rec.calls[0]["cwd"] == "/p"


def test_add_global_all(monkeypatch):
    rec = _Recorder({"ok": True, "action": "install"})
    monkeypatch.setattr(skills, "_run_askill", rec)
    _run(skills.add_skill("askill", "gh:o/r", scope="global", backends=["opencode"], all_skills=True))
    assert rec.calls[0]["args"] == ["add", "gh:o/r", "-g", "-a", "opencode", "--all", "-y"]


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
    assert rec.calls[0]["args"] == ["add", "./pkg", "--copy", "-y"]
    assert rec.calls[0]["cwd"] == "/p"


def test_add_with_skill_selector(monkeypatch):
    rec = _Recorder({"ok": True, "action": "install"})
    monkeypatch.setattr(skills, "_run_askill", rec)
    _run(skills.add_skill("askill", "./pkg", scope="project", project_dir="/p", skill="formatter", backends=["opencode"]))
    assert rec.calls[0]["args"] == ["add", "./pkg", "-a", "opencode", "--skill", "formatter", "-y"]


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
    assert rec.calls[0]["args"] == ["remove", "pdf-tools", "-a", "claude-code"]
    assert rec.calls[0]["cwd"] == "/p"


def test_remove_global(monkeypatch):
    rec = _Recorder({"ok": True})
    monkeypatch.setattr(skills, "_run_askill", rec)
    _run(skills.remove_skill("askill", "pdf-tools", scope="global"))
    assert rec.calls[0]["args"] == ["remove", "pdf-tools", "-g"]
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
