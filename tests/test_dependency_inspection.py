from __future__ import annotations

import asyncio
import copy
import itertools
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import httpx

from vibe import api


GROUPS = (
    ("askill",),
    ("avault",),
    ("show-runtime", "node"),
    ("model-hub-engine",),
    ("memory-package", "memory-runtime"),
    ("tmux",),
)


@pytest.fixture
def probes(monkeypatch):
    import core.tmux_runtime as tmux_runtime

    rows = {
        dep: {
            "id": dep,
            "kind": "runtime",
            "required": True,
            "installed": False,
            "status": "error",
            "version": None,
            "reason": "inspection_failed",
            "action_class": "operator_only",
        }
        for dep in api.DEPENDENCY_IDS
    }
    rows["askill"].update(installed=True, status="unknown")
    rows["avault"].update(installed=True, status="ready", version="0.1.6")
    rows["model-hub-engine"].update(
        installed=True, status="upgrade_required", version="v7.2.95", latest_version="v7.2.149"
    )
    rows["show-runtime"].update(installed=None, reason="runtime_install_inspection_failed")
    rows["node"].update(installed=None)
    rows["memory-package"].update(reason="memory_package_source_build", readiness="not_ready")
    rows["memory-runtime"].update(reason="memory_runtime_preparation_import_timeout")
    rows["tmux"].update(status="missing")
    calls = []

    def probe(group):
        def read(**kwargs):
            calls.append((group, kwargs))
            result = [copy.deepcopy(rows[dep]) for dep in group]
            return tuple(result) if len(result) > 1 else result[0]

        return read

    monkeypatch.setattr(api, "askill_update_status", probe(GROUPS[0]))
    monkeypatch.setattr(api, "avault_status", probe(GROUPS[1]))
    monkeypatch.setattr(api, "_show_runtime_dependencies_status", probe(GROUPS[2]))
    monkeypatch.setattr(api, "_model_hub_engine_dependency_status", probe(GROUPS[3]))
    monkeypatch.setattr(api, "_memory_dependencies_status", probe(GROUPS[4]))
    monkeypatch.setattr(tmux_runtime, "tmux_status", probe(GROUPS[5]))
    return calls


@pytest.mark.parametrize("selection", itertools.product((False, True), repeat=len(api.DEPENDENCY_IDS)))
def test_selective_checks_preserve_complete_rows_and_only_probe_their_owners(probes, selection):
    assert {dep for group in GROUPS for dep in group} == set(api.DEPENDENCY_IDS)
    requested = [dep for dep, selected in zip(api.DEPENDENCY_IDS, selection) if selected]
    complete = api.dependencies_status()
    probes.clear()

    result = api.dependencies_status(dependency_ids=requested)

    assert result == {"ok": True, "deps": [row for row in complete["deps"] if row["id"] in requested]}
    assert [group for group, _ in probes] == [group for group in GROUPS if set(group).intersection(requested)]


def test_coupled_rows_share_one_offline_inspection_and_duplicate_ids_do_not_repeat_it(probes):
    result = api.dependencies_status(
        offline=True,
        dependency_ids=["memory-runtime", "show-runtime", "memory-package", "node", "memory-runtime"],
    )
    assert [row["id"] for row in result["deps"]] == ["show-runtime", "memory-package", "memory-runtime", "node"]
    assert probes == [(GROUPS[2], {"offline": True}), (GROUPS[4], {"offline": True})]


def test_invalid_ids_are_rejected_before_any_probe(probes):
    with pytest.raises(ValueError, match="Unknown dependencies"):
        api.dependencies_status(dependency_ids=["askill", "not-a-dependency"])
    assert probes == []


def test_slow_check_cannot_hold_an_unrelated_dependency_request(monkeypatch, probes):
    entered, release = threading.Event(), threading.Event()

    def slow(**_kwargs):
        entered.set()
        assert release.wait(5)
        return {"installed": True, "status": "ready", "version": "0.1.15"}

    monkeypatch.setattr(api, "askill_update_status", slow)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(api.dependencies_status, dependency_ids=["askill"])
        try:
            assert entered.wait(2)
            ready = executor.submit(api.dependencies_status, dependency_ids=["avault"])
            assert ready.result(timeout=2)["deps"][0]["status"] == "ready"
            assert not pending.done()
        finally:
            release.set()
        assert pending.result(timeout=2)["deps"][0]["version"] == "0.1.15"


@pytest.mark.parametrize("query", ["id=avault", "id=memory-package&id=memory-runtime", ""])
def test_http_selection_reaches_the_shared_status_owner(monkeypatch, tmp_path, query):
    from vibe.ui_server import app

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    calls = []

    def inspect(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "deps": []}

    monkeypatch.setattr(api, "dependencies_status", inspect)
    response = app.test_client().get(f"/api/dependencies?{query}")
    assert response.status_code == 200
    expected = {"dependency_ids": [part.removeprefix("id=") for part in query.split("&")]} if query else {}
    assert calls == [expected]


def test_http_rejects_invalid_selection_before_inspecting(monkeypatch, tmp_path):
    from vibe.ui_server import app

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(api, "dependencies_status", lambda **_kwargs: pytest.fail("Unexpected probe"))
    response = app.test_client().get("/api/dependencies?id=avault&id=unknown")
    assert response.status_code == 400
    assert response.get_json() == {"ok": False, "error": "unknown_dependency"}


@pytest.mark.asyncio
async def test_http_serves_completed_dependency_while_another_probe_is_blocked(monkeypatch, tmp_path, probes):
    from vibe.ui_server import app

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    entered, release = threading.Event(), threading.Event()

    def slow(**_kwargs):
        entered.set()
        assert release.wait(10)
        return {"installed": True, "status": "ready", "version": "0.1.15"}

    monkeypatch.setattr(api, "askill_update_status", slow)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        pending = asyncio.create_task(client.get("/api/dependencies?id=askill"))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            fast = await asyncio.wait_for(client.get("/api/dependencies?id=avault"), timeout=3)
            assert fast.status_code == 200
            assert fast.json()["deps"][0]["id"] == "avault"
            assert not pending.done()
        finally:
            release.set()
            slow_result = await asyncio.wait_for(pending, timeout=3)
        assert slow_result.status_code == 200
