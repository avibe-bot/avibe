"""Real consuming paths must not turn staging/legacy metadata into execution."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.handlers.model_hub.capability_evidence import CapabilityEvidenceStore, wire_stamp
from core.handlers.model_hub.hosted_tools import UNKNOWN_ENGINE_RELEASE
from tests.test_model_hub_engine_lifecycle import owner_fixture, signed_candidate
from tests.test_model_hub_l3 import _source
from tests.test_model_hub_vibey_consumer import _vibey_service


def test_no_controller_api_ensure_stages_required_c_and_changes_no_selection_or_hint(tmp_path, monkeypatch):
    from vibe import api

    owner, supervisor, _baseline = owner_fixture(tmp_path, monkeypatch)
    candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    owner.activate(candidate)
    manager = owner.manager
    manager.offline = True
    hint = manager.runtime_dir / "current.json"
    before_hint = hint.read_bytes()
    before = owner.journal.load()
    monkeypatch.setattr("vibe.runtime.resolve_service_owner_pid", lambda **_kwargs: None)
    monkeypatch.setattr("vibe.internal_client.default_socket_path", lambda: tmp_path / "no-controller.sock")
    monkeypatch.setattr("vibe.model_hub_runtime.installer.EngineRuntimeManager", lambda **_kwargs: manager)
    monkeypatch.setattr("vibe.model_hub_runtime.installer.subprocess.run", lambda *_args, **_kwargs: pytest.fail("stage-only executes no version probe"))
    monkeypatch.setattr("vibe.model_hub_runtime.supervisor.subprocess.Popen", lambda *_args, **_kwargs: pytest.fail("stage-only starts no engine"))

    result = api.ensure_model_hub_engine_installed(force=True, offline=True)

    assert result["ok"] and result["reason"] == "waiting_for_controller"
    assert result["generation"]["asset_release_tag"] == candidate.asset_release_tag
    after = owner.journal.load()
    assert after["active"] == before["active"] and after["previous"] == before["previous"]
    assert after["health"] == before["health"] and after["pending"] is None
    assert hint.read_bytes() == before_hint
    assert supervisor.running is None


def test_dependency_status_reports_required_c_and_blocked_execution_not_legacy_b(tmp_path, monkeypatch):
    from vibe import api

    owner, _supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    owner.activate(candidate)
    owner.block("engine_withdrawn")
    monkeypatch.setattr("vibe.model_hub_runtime.installer.EngineRuntimeManager", lambda **_kwargs: owner.manager)

    result = api._model_hub_engine_dependency_status()

    assert result["installed"] is True and result["status"] == "error"
    assert result["version"] == candidate.runtime_version
    assert result["engine_lifecycle"]["committed"] == candidate.document()
    assert result["engine_lifecycle"]["reason"] == "engine_withdrawn"
    assert result["engine_lifecycle"]["running"] is None
    assert json.loads((owner.manager.runtime_dir / "current.json").read_bytes())["install_dir"] == baseline["install_dir"]


def test_doctor_repair_reports_files_staged_not_execution_repaired(monkeypatch):
    from vibe import cli

    calls = []

    def ensure(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "changed": True, "state": "staged", "reason": "waiting_for_controller", "version": "v8.0.63"}

    monkeypatch.setattr("vibe.api.ensure_model_hub_engine_installed", ensure)
    result = cli._repair_model_hub_engine()
    assert calls == [{"force": True}]
    assert result["status"] == "skipped" and result["state"] == "staged"
    assert result["reason"] == "waiting_for_controller"
    assert "controller" in result["message"].lower() or "控制器" in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted", [None, "model-hub-engine-v8.0.63-2"])
async def test_success_persisted_after_swap_carries_admitted_identity_not_new_or_packaged(tmp_path, admitted):
    source = _source("src_primary01", "xAI key", vendor="xai", model_id="grok-4.6")
    service = _vibey_service(tmp_path, [source])
    service.adapter.engine_release = lambda: "model-hub-engine-v8.0.64-1"

    await service.record_hosted_success(source, "grok-4.6", "x_search", "executor", engine_release=admitted)

    expected = wire_stamp(
        engine=admitted or UNKNOWN_ENGINE_RELEASE, avibe=service.avibe_release,
        source=source, model_id="grok-4.6",
    )
    records = service.capability_evidence.records()
    assert len(records) == 1 and records[0].wire == expected
    assert records[0].wire != service.hosted_evidence().wire_for(source, "grok-4.6")
    # Read back the actual file, not only the in-memory writer snapshot.
    persisted = CapabilityEvidenceStore(Path(service.capability_evidence.path)).records()
    assert len(persisted) == 1 and persisted[0].wire == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["newer_missing", "newer_present", "minimum", "no_root", "compatible", "corrupt_c"])
async def test_package_transition_is_owner_convergence_not_candidate_or_repair_repin(tmp_path, monkeypatch, change):
    import vibe

    from core.model_hub_engine_channel import TrustRoots
    from tests.test_model_hub_engine_lifecycle import fixture_manifest
    from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter

    monkeypatch.setattr(vibe, "__version__", "3.2.6")
    owner, supervisor, _baseline = owner_fixture(tmp_path, monkeypatch)
    candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    owner.activate(candidate)
    prior_package = owner.journal.load()["authorized_package"]
    monkeypatch.setattr(vibe, "__version__", "3.2.7" if change != "minimum" else "3.2.5")
    owner.channel._avibe_version = vibe.__version__
    if change.startswith("newer_"):
        raw = fixture_manifest(tmp_path / "new-package", version="v8.0.70")
        monkeypatch.setattr(owner.manager, "bundled_manifest_bytes", lambda: raw)
        if change == "newer_present":
            assert owner.manager.stage_manifest(raw)["ok"]
    elif change == "no_root":
        owner.channel.roots = TrustRoots(roots=(), sequence_floor=0)
    elif change == "corrupt_c":
        (Path(candidate.install_dir) / candidate.bin_path).write_bytes(b"corrupt test-owned C")
    # Reauthorization cannot depend on whether the new baseline is cached.
    owner.recover()
    package_change = change not in {"compatible", "corrupt_c"}
    if change == "newer_missing":
        assert owner.journal.load()["active"] == candidate.document()
        assert owner.journal.load()["authorized_package"] == prior_package
        assert not owner._ready
    if change.startswith("newer_") and change != "newer_present":
        # Even here direct repair stages the exact C, never selects B_new.
        assert owner.stage_required()["generation"] == candidate.document()
        assert owner.journal.load()["active"] == candidate.document()
    supervisor.installer = owner.manager
    supervisor.status = lambda: {"status": {
        "health": "not_started" if owner._ready else "down",
        "verified": owner._ready, "installed_version": owner.manager.status().get("version"),
    }}  # process/status protocol fixture; actual staging/channel/owner below
    adapter = CLIProxyEngineAdapter(supervisor=supervisor, state_store=object(), routine_adoption=lambda: False)
    await adapter._reconcile_engine(explicit=False)
    value = owner.journal.load()
    assert value["blocked"] is None and owner._ready
    if package_change:
        assert value["active"]["origin"] == "bundled"
        assert value["active"]["runtime_version"] == ("v8.0.70" if change.startswith("newer_") else "v8.0.30")
        assert value["previous"] == candidate.document()
    else:
        assert value["active"]["asset_release_tag"] == candidate.asset_release_tag
        assert value["active"]["origin"] == "channel"
    assert supervisor.running is None  # no package transition starts stopped CPA
    assert owner.journal.load()["authorized_package"] == owner.manager.package_identity()
