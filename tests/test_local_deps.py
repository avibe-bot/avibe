"""Hermetic tests for the askill local-dependency helpers in vibe/api.py.

The subprocess / path-resolution boundary is monkeypatched, so these run
without askill, npm, or the network — they pin the install command
construction, the idempotency of ``ensure_askill_installed``, and the status
shape.
"""

from __future__ import annotations

import pytest

from vibe import api


def test_install_askill_uses_official_curl_installer(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: f"/usr/bin/{b}" if b in {"curl", "bash"} else None)

    def fake_run(name, cmd, _trunc, *, mode="install", env=None):
        captured.update(name=name, cmd=cmd, mode=mode)
        return {"ok": True, "path": "/usr/local/bin/askill", "output": ""}

    monkeypatch.setattr(api, "_run_install_command", fake_run)
    out = api.install_askill()
    assert out["ok"]
    assert captured["name"] == "askill"
    assert captured["cmd"][:2] == ["bash", "-c"]
    assert "curl -fsSL https://askill.sh | sh" in captured["cmd"][2]


def test_install_askill_unsupported_without_curl(monkeypatch):
    # No curl/bash (e.g. Windows): no broken npm fallback — a clear manual
    # message pointing at askill.sh, and _run_install_command is never invoked.
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: None)
    monkeypatch.setattr(api, "_run_install_command", lambda *a, **k: pytest.fail("should not install"))
    out = api.install_askill()
    assert out["ok"] is False
    assert "askill.sh" in out["message"]


def test_ensure_askill_idempotent_when_present(monkeypatch):
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/usr/local/bin/askill")
    flag = {"installed": False}
    monkeypatch.setattr(api, "install_askill", lambda: flag.__setitem__("installed", True) or {"ok": True})
    out = api.ensure_askill_installed()
    assert out == {"ok": True, "installed": True, "changed": False, "path": "/usr/local/bin/askill"}
    assert flag["installed"] is False  # never installed when already present


def test_ensure_askill_installs_when_missing(monkeypatch):
    # Missing on the first check, resolvable after install.
    seen = {"n": 0}

    def fake_resolve(_b):
        seen["n"] += 1
        return None if seen["n"] == 1 else "/x/askill"

    monkeypatch.setattr(api, "resolve_cli_path", fake_resolve)
    monkeypatch.setattr(api, "install_askill", lambda: {"ok": True})
    out = api.ensure_askill_installed()
    assert out["ok"] and out["installed"] and out["changed"] and out["path"] == "/x/askill"


def test_ensure_askill_install_not_discoverable_is_failure(monkeypatch):
    # Installer exits 0 but the binary never resolves on the service PATH —
    # must NOT report success, or the UI claims installed while skills 404.
    monkeypatch.setattr(api, "resolve_cli_path", lambda _b: None)
    monkeypatch.setattr(api, "install_askill", lambda: {"ok": True})
    out = api.ensure_askill_installed()
    assert out["ok"] is False and out["installed"] is False and out["path"] is None


def test_ensure_askill_force_reinstalls_even_when_present(monkeypatch):
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/usr/local/bin/askill")
    flag = {"installed": False}
    monkeypatch.setattr(api, "install_askill", lambda: flag.__setitem__("installed", True) or {"ok": True})
    api.ensure_askill_installed(force=True)
    assert flag["installed"] is True


def test_ensure_askill_skips_when_install_already_running():
    assert api._ASKILL_INSTALL_LOCK.acquire(blocking=False) is True
    try:
        out = api.ensure_askill_installed(force=True)
    finally:
        api._ASKILL_INSTALL_LOCK.release()

    assert out["ok"] is False
    assert out["skipped"] is True
    assert out["reason"] == "askill_install_already_running"
    assert "already running" in out["message"]


def test_askill_status_missing(monkeypatch):
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: None)
    s = api.askill_status()
    assert s["installed"] is False and s["status"] == "missing" and s["version"] is None


def test_askill_status_present_parses_version(monkeypatch):
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/x/askill")
    monkeypatch.setattr(api, "_command_env_for", lambda p: {})
    monkeypatch.setattr(api, "isolated_subprocess_kwargs", lambda: {})

    class _R:
        returncode = 0
        stdout = "askill 0.1.13\n"
        stderr = ""

    monkeypatch.setattr(api.subprocess, "run", lambda *a, **k: _R())
    s = api.askill_status()
    assert s["installed"] and s["version"] == "0.1.13" and s["status"] == "ready"


def test_install_avault_uses_existing_configured_binary(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "/opt/avault/bin/avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/opt/avault/bin/avault" if b == "/opt/avault/bin/avault" else None)

    out = api.install_avault()

    assert out["ok"] is True
    assert out["path"] == "/opt/avault/bin/avault"


def test_install_avault_missing_is_clear_stub_failure(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda _b: None)

    out = api.install_avault()

    assert out["ok"] is False
    assert "agents.avault.cli_path" in out["message"]


def test_avault_resolves_path_fallback_when_configured_path_missing(monkeypatch):
    seen = []

    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "/missing/avault")

    def fake_resolve(binary):
        seen.append(binary)
        return "/usr/local/bin/avault" if binary == "avault" else None

    monkeypatch.setattr(api, "resolve_cli_path", fake_resolve)

    assert api._resolve_avault_cli_path() == "/usr/local/bin/avault"
    assert seen == ["/missing/avault", "avault"]


def test_ensure_avault_idempotent_when_present(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/usr/local/bin/avault")
    flag = {"installed": False}
    monkeypatch.setattr(api, "install_avault", lambda: flag.__setitem__("installed", True) or {"ok": True})

    out = api.ensure_avault_installed()

    assert out == {"ok": True, "installed": True, "changed": False, "path": "/usr/local/bin/avault"}
    assert flag["installed"] is False


def test_ensure_avault_force_rechecks_existing_binary(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/usr/local/bin/avault")
    flag = {"installed": False}
    monkeypatch.setattr(api, "install_avault", lambda: flag.__setitem__("installed", True) or {"ok": True})

    out = api.ensure_avault_installed(force=True)

    assert flag["installed"] is True
    assert out["ok"] is True
    assert out["installed"] is True
    assert out["changed"] is False


def test_avault_status_missing(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: None)
    s = api.avault_status()
    assert s["id"] == "avault"
    assert s["installed"] is False and s["status"] == "missing" and s["version"] is None


def test_avault_status_present_parses_version(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/x/avault")
    monkeypatch.setattr(api, "_command_env_for", lambda p: {})
    monkeypatch.setattr(api, "isolated_subprocess_kwargs", lambda: {})

    class _R:
        returncode = 0
        stdout = "avault 0.0.1\n"
        stderr = ""

    monkeypatch.setattr(api.subprocess, "run", lambda *a, **k: _R())
    s = api.avault_status()
    assert s["installed"] and s["version"] == "0.0.1" and s["status"] == "ready"


def test_askill_update_status_compares_latest(monkeypatch):
    monkeypatch.setattr(
        api,
        "askill_status",
        lambda: {"id": "askill", "installed": True, "version": "0.1.13", "status": "ready", "path": "/x"},
    )
    monkeypatch.setattr(api, "_cached_latest_askill", lambda: "0.1.14")

    s = api.askill_update_status()

    assert s["latest_version"] == "0.1.14"
    assert s["has_update"] is True
    assert s["auto_update"] is True


def test_reconcile_askill_auto_update_installs_when_missing(monkeypatch):
    monkeypatch.setattr(
        api,
        "askill_update_status",
        lambda **_: {"id": "askill", "installed": False, "version": None, "status": "missing", "latest_version": "0.1.14"},
    )
    calls = []

    def fake_ensure(force=False):
        calls.append(force)
        return {"ok": True, "installed": True, "changed": True, "path": "/x/askill"}

    monkeypatch.setattr(api, "ensure_askill_installed", fake_ensure)

    out = api.reconcile_askill_auto_update()

    assert calls == [False]
    assert out["ok"] is True and out["action"] == "install"


def test_reconcile_askill_auto_update_refreshes_when_newer(monkeypatch):
    monkeypatch.setattr(
        api,
        "askill_update_status",
        lambda **_: {
            "id": "askill",
            "installed": True,
            "version": "0.1.13",
            "status": "ready",
            "latest_version": "0.1.14",
            "has_update": True,
        },
    )
    calls = []

    def fake_ensure(force=False):
        calls.append(force)
        return {"ok": True, "installed": True, "changed": True, "path": "/x/askill"}

    monkeypatch.setattr(api, "ensure_askill_installed", fake_ensure)

    out = api.reconcile_askill_auto_update()

    assert calls == [True]
    assert out["ok"] is True
    assert out["action"] == "update"
    assert out["from_version"] == "0.1.13"
    assert out["latest_version"] == "0.1.14"


def test_reconcile_askill_auto_update_refreshes_when_current_version_unknown(monkeypatch):
    monkeypatch.setattr(
        api,
        "askill_update_status",
        lambda **_: {
            "id": "askill",
            "installed": True,
            "version": None,
            "status": "unknown",
            "latest_version": "0.1.14",
            "has_update": False,
        },
    )
    calls = []

    def fake_ensure(force=False):
        calls.append(force)
        return {"ok": True, "installed": True, "changed": True, "path": "/x/askill"}

    monkeypatch.setattr(api, "ensure_askill_installed", fake_ensure)

    out = api.reconcile_askill_auto_update()

    assert calls == [True]
    assert out["ok"] is True
    assert out["action"] == "refresh_unknown_version"
    assert out["latest_version"] == "0.1.14"


def test_reconcile_askill_auto_update_skips_when_disabled(monkeypatch):
    monkeypatch.setenv("VIBE_ASKILL_AUTO_UPDATE", "0")
    monkeypatch.setattr(api, "askill_update_status", lambda **_: pytest.fail("should not probe"))

    out = api.reconcile_askill_auto_update()

    assert out == {"ok": True, "skipped": True, "reason": "askill_auto_update_disabled"}


def test_dependencies_status_shape(monkeypatch):
    monkeypatch.setattr(
        api,
        "askill_update_status",
        lambda **_: {
            "id": "askill",
            "installed": True,
            "version": "0.1.13",
            "latest_version": None,
            "has_update": False,
            "status": "ready",
            "path": "/x",
        },
    )
    monkeypatch.setattr(
        api,
        "avault_status",
        lambda: {"id": "avault", "installed": True, "version": "0.0.1", "status": "ready", "path": "/x/avault"},
    )
    import core.show_runtime as srt_mod

    class _Mgr:
        def status(self):
            return {"installed": True, "manifest": {"runtime_version": "1.4.0"}, "node_available": True, "node_version": "20.11"}

    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: _Mgr())
    out = api.dependencies_status()
    assert out["ok"]
    by = {d["id"]: d for d in out["deps"]}
    assert list(by) == ["askill", "avault", "show-runtime", "node"]
    assert by["askill"]["status"] == "ready" and by["askill"]["version"] == "0.1.13" and by["askill"]["required"]
    assert by["askill"]["latest_version"] is None and by["askill"]["has_update"] is False
    assert by["avault"]["status"] == "ready" and by["avault"]["version"] == "0.0.1" and by["avault"]["required"]
    assert by["avault"]["latest_version"] is None and by["avault"]["has_update"] is False
    assert by["show-runtime"]["installed"] and by["show-runtime"]["version"] == "1.4.0"
    assert by["node"]["installed"] and by["node"]["version"] == "20.11"


def test_dependencies_status_node_unsupported_not_ready(monkeypatch):
    # Node present but below the runtime minimum (node_supported False) -> not ready.
    monkeypatch.setattr(
        api,
        "askill_update_status",
        lambda **_: {"id": "askill", "installed": True, "version": "0.1.13", "status": "ready", "path": "/x"},
    )
    monkeypatch.setattr(
        api,
        "avault_status",
        lambda: {"id": "avault", "installed": True, "version": "0.0.1", "status": "ready", "path": "/x/avault"},
    )
    import core.show_runtime as srt_mod

    class _Mgr:
        def status(self):
            return {"installed": False, "manifest": None, "node_available": True, "node_supported": False, "node_version": "16.0"}

    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: _Mgr())
    by = {d["id"]: d for d in api.dependencies_status()["deps"]}
    assert by["node"]["installed"] is False and by["node"]["status"] == "missing"


def test_reconcile_startup_dependencies_installs_askill_and_defers_runtime_prepare(monkeypatch):
    askill_calls = []
    avault_calls = []

    def fake_ensure(force=False):
        askill_calls.append(force)
        return {"ok": True, "installed": True, "changed": True, "path": "/x/askill"}

    monkeypatch.setattr(api, "ensure_askill_installed", fake_ensure)

    def fake_ensure_avault(force=False):
        avault_calls.append(force)
        return {"ok": True, "installed": True, "changed": False, "path": "/x/avault"}

    monkeypatch.setattr(api, "ensure_avault_installed", fake_ensure_avault)

    import core.show_runtime as srt_mod

    class _Mgr:
        def __init__(self):
            self.prepared = []

        def status(self):
            return {
                "installed": False,
                "manifest": {"runtime_version": "1.4.0"},
                "node_available": True,
                "node_supported": True,
                "node_version": "22.12.0",
            }

        def prepare(self, *, force=False):
            self.prepared.append(force)
            return {"ok": True, "reason": None}

    manager = _Mgr()
    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: manager)

    out = api.reconcile_startup_dependencies()

    assert out["ok"] is True
    assert askill_calls == [False]
    assert avault_calls == [False]
    assert manager.prepared == []
    assert out["node"]["status"] == "ready"
    assert out["show_runtime"] == {"ok": True, "status": "pending_prewarm", "reason": None}


def test_reconcile_startup_dependencies_does_not_prepare_runtime_without_node(monkeypatch):
    monkeypatch.setattr(api, "ensure_askill_installed", lambda force=False: {"ok": True, "installed": True})
    monkeypatch.setattr(api, "ensure_avault_installed", lambda force=False: {"ok": True, "installed": True})

    import core.show_runtime as srt_mod

    class _Mgr:
        def status(self):
            return {"installed": False, "node_available": False, "node_version": None}

        def prepare(self, *, force=False):
            raise AssertionError("runtime must not prepare without Node")

    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: _Mgr())

    out = api.reconcile_startup_dependencies()

    assert out["ok"] is False
    assert out["node"]["status"] == "missing"
    assert out["show_runtime"] == {"ok": False, "status": "skipped", "reason": "runtime_node_missing"}


def test_reconcile_startup_dependencies_can_be_disabled(monkeypatch):
    monkeypatch.setenv("VIBE_STARTUP_DEPENDENCY_RECONCILE", "0")
    monkeypatch.setattr(api, "ensure_askill_installed", lambda force=False: pytest.fail("should not reconcile"))
    monkeypatch.setattr(api, "ensure_avault_installed", lambda force=False: pytest.fail("should not reconcile"))

    out = api.reconcile_startup_dependencies()

    assert out == {"ok": True, "skipped": True, "reason": "disabled"}


def test_startup_show_page_prewarm_targets_recent_non_offline(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from config import paths
    from core.show_pages import ShowPageStore

    paths.ensure_data_dirs()
    store = ShowPageStore()
    try:
        store.ensure("ses-old")
        store.update_visibility("ses-public", "public")
        store.update_visibility("ses-offline", "offline")
        store.ensure("ses-new")
    finally:
        store.close()

    out = api.startup_show_page_prewarm_targets(limit=2)

    assert out["limit"] == 2
    assert [page["session_id"] for page in out["pages"]] == ["ses-new", "ses-public"]
    assert out["pages"][1]["visibility"] == "public"
    assert out["pages"][1]["base_path"].startswith("/p/")


def test_startup_show_page_prewarm_limit_env(monkeypatch):
    monkeypatch.setenv("VIBE_STARTUP_SHOW_PAGE_PREWARM_LIMIT", "0")
    assert api.startup_show_page_prewarm_limit() == 0

    monkeypatch.setenv("VIBE_STARTUP_SHOW_PAGE_PREWARM_LIMIT", "99")
    assert api.startup_show_page_prewarm_limit() == 10


def test_start_dependency_install_job_rejects_unknown():
    assert api.start_dependency_install_job("bogus")["ok"] is False


def test_start_dependency_install_job_runs_askill(monkeypatch):
    import time as _t

    flag = {"called": False}

    def fake_ensure(force=False):
        flag["called"] = True
        return {"ok": True, "installed": True, "changed": True, "path": "/x/askill"}

    monkeypatch.setattr(api, "ensure_askill_installed", fake_ensure)
    job = api.start_dependency_install_job("askill")
    # Don't assert status=="running": an instant (mocked) worker can finish
    # before the snapshot is taken. Real installs are slow, so the UI still
    # observes "running" + polls. Verify completion via the poller below.
    assert job["ok"] and job["backend"] == "askill" and job.get("job_id")
    cur = job
    for _ in range(100):
        cur = api.get_agent_install_job(job["job_id"], backend="askill")
        if cur.get("status") != "running":
            break
        _t.sleep(0.02)
    assert flag["called"] is True
    assert cur["status"] == "succeeded" and cur["ok"] is True


def test_start_dependency_install_job_runs_avault(monkeypatch):
    import time as _t

    flag = {"called": False}

    def fake_ensure(force=False):
        flag["called"] = True
        return {"ok": True, "installed": True, "changed": False, "path": "/x/avault"}

    monkeypatch.setattr(api, "ensure_avault_installed", fake_ensure)
    job = api.start_dependency_install_job("avault")
    assert job["ok"] and job["backend"] == "avault" and job.get("job_id")
    cur = job
    for _ in range(100):
        cur = api.get_agent_install_job(job["job_id"], backend="avault")
        if cur.get("status") != "running":
            break
        _t.sleep(0.02)
    assert flag["called"] is True
    assert cur["status"] == "succeeded" and cur["ok"] is True
