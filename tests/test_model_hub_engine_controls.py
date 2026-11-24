"""The dependency API observes the owner, and explicit requests have stable receipts."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import threading

import pytest

from config import paths
from config.v2_config import V2Config
from core.handlers.model_hub import ModelHubError
from tests.test_model_hub_engine_lifecycle import owner_fixture, signed_candidate
from tests.ui_server_test_helpers import csrf_headers
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.controls import EngineJobs, auto_update_enabled


def test_policy_endpoint_changes_only_engine_setting_and_survives_legacy_read(tmp_path, monkeypatch):
    """A permissive old section must ignore the new key without touching source data."""
    from vibe.ui_server import app

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = V2Config.default()
    config.save()
    path = paths.get_config_path()
    before = json.loads(path.read_bytes())
    client = app.test_client()
    url = "/api/dependencies/model-hub-engine/auto-update"
    assert client.post(url, json={"enabled": False}).status_code == 403
    headers = csrf_headers(client)
    assert client.post(url, json={"enabled": "false"}, headers=headers).status_code == 422
    r = client.post(url, json={"enabled": False}, headers=headers)
    assert r.status_code == 200 and r.get_json() == {"auto_update": False, "ok": True}
    after = json.loads(path.read_bytes())
    before["update"]["model_hub_engine_auto_update"] = False
    assert after == before

    @dataclass
    class ReleasedUpdateConfig:  # v3.2.7 update-section constructor
        auto_update: bool = True
        check_interval_minutes: int = 60
        idle_minutes: int = 30
        notify_admins: bool = True
        prereleases: bool = False

    with monkeypatch.context() as old:
        old.setattr("config.v2_config.UpdateConfig", ReleasedUpdateConfig)
        assert not hasattr(V2Config.load().update, "model_hub_engine_auto_update")
    assert not auto_update_enabled()
    del after["update"]["model_hub_engine_auto_update"]  # existing pre-feature config, nothing to migrate
    assert V2Config.from_payload(after).update.model_hub_engine_auto_update is True


@pytest.mark.asyncio
async def test_pollable_check_never_adopts_or_claims_active_and_update_intent_is_not_lost(tmp_path, monkeypatch):
    owner, supervisor, _ = owner_fixture(tmp_path, monkeypatch)
    candidate, _, _, envelope = signed_candidate(owner, tmp_path)
    adapter = CLIProxyEngineAdapter(supervisor=supervisor, state_store=object(), routine_adoption=lambda: False)
    entered, release = threading.Event(), threading.Event()

    def fetch():
        entered.set()
        assert release.wait(8)
        return envelope

    owner.channel._fetch = fetch
    before = owner.journal.load()["active"]
    # Status cannot fetch metadata, adopt or launch. Staged files aren't running.
    snapshot = await adapter.engine_status()
    assert snapshot["active_release"] is None and snapshot["available_release"] == candidate.asset_release_tag
    assert not entered.is_set() and not snapshot["auto_update"]
    job = await adapter.request_engine_operation()
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert (await adapter.request_engine_operation())["operation_id"] == job["job_id"]
        with pytest.raises(ModelHubError) as busy:
            await adapter.request_engine_operation(update=True)
        assert busy.value.status == 409 and busy.value.data["operation_id"] == job["job_id"]
        assert (await adapter.engine_job(job["job_id"]))["status"] == "running"
    finally:
        release.set()
        await adapter._engine_operation_task
    terminal = await adapter.engine_job(job["job_id"])
    assert terminal["status"] == "succeeded" and terminal["update_state"] == "available"
    assert EngineJobs(owner.manager.runtime_dir).get(job["job_id"]) == terminal
    assert owner.journal.load()["active"] == before and supervisor.running is None
    # Explicit update can adopt while policy is off, but not start a stopped engine.
    new = await adapter.request_engine_operation(update=True)
    await adapter._engine_operation_task
    assert (await adapter.engine_job(new["job_id"]))["update_state"] == "applied"
    assert owner.journal.load()["active"] == candidate.document() and supervisor.running is None
    after = await adapter.engine_status()
    assert after["active_release"] is None and after["available_release"] is None
    assert after["last_result"] == "applied"


def test_interrupted_receipt_is_failed_not_replayed(tmp_path):
    jobs = EngineJobs(tmp_path)
    jobs.store(jobs.started("engine-lost", True))
    assert EngineJobs(tmp_path).get("engine-lost")["update_reason"] == "operation_interrupted"


@pytest.mark.parametrize("action", ["check", "update"])
def test_channel_http_requests_refuse_without_controller_not_direct_install(action, monkeypatch, tmp_path):
    from vibe.ui_server import app

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    client = app.test_client()
    path = f"/api/dependencies/model-hub-engine/{action}"
    assert client.post(path).status_code == 403
    response = client.post(path, headers=csrf_headers(client))
    assert response.status_code == 503 and response.get_json()["update_state"] == "waiting_for_controller"
