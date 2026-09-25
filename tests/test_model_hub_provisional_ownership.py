"""Write-ahead takeover ownership, using fixture homes and engine doubles."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from core.handlers.model_hub.service import ModelHubError
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import (
    _isolate_native_home,
    _service,
    _write,
    _write_claude_oauth,
)


def _native(monkeypatch, tmp_path, kind):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    if kind == "oauth":
        _write_claude_oauth(home)
        path = home / ".claude/.credentials.json"
    else:
        path = home / ".config/opencode/opencode.json"
        _write(path, '{"provider":{"openai":{"options":{"apiKey":"fixture-密钥"}}}}')
    service, store, adapter = _service(tmp_path, migration_home=home)
    rows = service.migration_scan()["items"]
    assert len(rows) == 1
    store.config.agents[rows[0]["backend"]].mode = "direct"
    return service, store, adapter, path, [rows[0]["id"]]


@pytest.mark.parametrize("kind", ["oauth", "api_key", "observation"])
def test_provision_failure_before_return_keeps_durable_cleanup_owner(monkeypatch, tmp_path, kind):
    service, store, adapter, native, ids = _native(monkeypatch, tmp_path, kind)
    original = native.read_bytes()
    method = {
        "oauth": "provision_oauth_credential",
        "api_key": "provision_credential",
        "observation": "provision_transient_credential",
    }[kind]
    provision = getattr(adapter, method)
    observed = []

    async def write_then_lose_return(*args, on_reserved):
        def reserve(ref):
            on_reserved(ref)
            entry, = service.revocations.list()
            assert entry.credential_ref == ref
            if kind == "observation":
                assert entry.source_id == "observation"
            elif kind == "oauth":
                assert entry.source_id == args[0]
            else:
                assert entry.source_id.startswith("src_")
            assert ":migration:" not in entry.source_id
            assert service.migration_journal.load() is None
            observed.append(entry)
        ref = await provision(*args, on_reserved=reserve)
        adapter.fail_revoke_refs.add(ref)
        # The engine wrote the secret, but its caller never received the ref.
        raise RuntimeError("fixture connection lost before provision returned")

    setattr(adapter, method, write_then_lose_return)
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids, clean_api_keys=True))
    assert len(observed) == 1
    assert service.revocations.list() == observed
    assert not store.config.sources
    assert native.read_bytes() == original
    assert service.migration_journal.load() is None
    assert not adapter.activated
    # A fresh service discovers the opaque ref without the lost in-memory list.
    recovered, _, _ = _service(tmp_path, migration_home=tmp_path / "native")
    recovered.store, recovered.adapter = store, adapter
    adapter.fail_revoke_refs.clear()
    asyncio.run(recovered._ensure_engine_synced())
    assert recovered.revocations.list() == []
    assert observed[0].credential_ref in adapter.revoked + adapter.transient_revoked
    assert native.read_bytes() == original


@pytest.mark.parametrize("kind", ["oauth", "api_key"])
def test_failed_journal_durability_prevents_secret_provision(monkeypatch, tmp_path, kind):
    service, store, adapter, native, ids = _native(monkeypatch, tmp_path, kind)
    original = native.read_bytes()

    def fail_flush():
        raise OSError("fixture directory fsync failed")

    monkeypatch.setattr(service.revocations, "_sync_directory", fail_flush)
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids, clean_api_keys=True))
    assert not adapter.provisioned and not adapter.oauth_provisioned and not adapter.transient_refs
    assert not store.config.sources
    assert native.read_bytes() == original
    assert service.migration_journal.load() is None
    # A durable row may exist after rename, but no secret has been written.
    assert "fixture-密钥" not in service.revocations.path.read_text()


def test_directory_flush_is_retried_for_an_existing_provisional_owner(monkeypatch, tmp_path):
    from core.handlers.model_hub.revocations import CredentialRevocationJournal

    journal = CredentialRevocationJournal(tmp_path / "revocations.json")
    flushed = []

    def fail_once():
        flushed.append(True)
        if len(flushed) == 1:
            raise OSError("fixture directory failure")

    monkeypatch.setattr(journal, "_sync_directory", fail_once)
    with pytest.raises(OSError):
        journal.add("src_fixture", "cred_fixture")
    journal.add("src_fixture", "cred_fixture")
    assert len(flushed) == 2
    assert len(journal.list()) == 1


def test_exposed_grant_is_owned_by_its_current_source_during_revocation_replay(monkeypatch, tmp_path):
    service, store, adapter, native, ids = _native(monkeypatch, tmp_path, "oauth")

    async def validation_pending(ref):
        raise RuntimeError("fixture upstream witness unavailable")

    adapter.validate_oauth_credential = validation_pending
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids, clean_api_keys=True))
    record = service.migration_journal.load()
    assert record["phase"] == "exposed"
    source, = store.config.sources
    entry, = service.revocations.list()
    assert (entry.source_id, entry.credential_ref) == (source.id, source.credential_ref)
    assert not native.exists()
    asyncio.run(service._ensure_engine_synced())
    assert not adapter.revoked
    assert service.revocations.list() == []
    assert service.migration_journal.load()["phase"] == "exposed"
    # The remaining transaction owns forward recovery, never old-native restore.
    assert json.loads(service.migration_journal.path.read_text())["credentials"][0]["credential_ref"] == source.credential_ref


@pytest.mark.parametrize("kind", ["oauth", "api_key"])
def test_prepared_save_uncertain_outcome_keeps_its_provisional_grant(
    monkeypatch, tmp_path, kind,
):
    service, store, adapter, native, ids = _native(monkeypatch, tmp_path, kind)
    before = native.read_bytes()
    save = service.migration_journal.save

    def committed_then_failed(record):
        save(record)
        raise OSError("fixture failure after journal replacement")

    monkeypatch.setattr(service.migration_journal, "save", committed_then_failed)
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids, clean_api_keys=True))
    pending = service.migration_journal.load()
    assert pending["phase"] == "prepared"
    assert not adapter.revoked
    assert native.read_bytes() == before
    assert not store.config.sources
    # The prepared record may be durable even though save raised. Recovery
    # owns these refs; a local finally must not revoke them from underneath it.
    refs = {item["credential_ref"] for item in pending["credentials"]}
    assert refs <= {entry.credential_ref for entry in service.revocations.list()}
    # Another Hub consumer may reconcile before explicit migration recovery.
    # It must not treat a prepared transaction's not-yet-bound refs as orphans.
    asyncio.run(service._ensure_engine_synced())
    assert not adapter.revoked
    assert refs <= {entry.credential_ref for entry in service.revocations.list()}
    recovered, _, _ = _service(tmp_path, migration_home=tmp_path / "native")
    recovered.store, recovered.adapter = store, adapter
    asyncio.run(recovered.recover_runtime_intent())
    assert recovered.migration_journal.load() is None
    assert not recovered.revocations.list()
    assert {source.credential_ref for source in store.config.sources} == refs
    assert not adapter.revoked


@pytest.mark.parametrize("kind", ["oauth", "api_key"])
def test_real_engine_material_is_recovered_without_a_prepared_takeover_record(
    monkeypatch, tmp_path, kind,
):
    from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
    from vibe.model_hub_runtime.state import EngineStateStore

    service, store, adapter, native, ids = _native(monkeypatch, tmp_path, kind)
    original = native.read_bytes()
    state = EngineStateStore(tmp_path / "engine")
    runtime = CLIProxyEngineAdapter(
        supervisor=SimpleNamespace(
            state_store=state, invalidate_configs=lambda: None,
            client_if_running=lambda: None,
        ),
        state_store=state,
    )
    for method in ("provision_credential", "provision_transient_credential", "provision_oauth_credential"):
        setattr(adapter, method, getattr(runtime, method))

    async def unavailable_cleanup(ref):
        # Simulate process loss: no in-process cleanup can reclaim these bytes.
        raise RuntimeError("fixture cleanup unavailable until restart")

    adapter.revoke_credential = unavailable_cleanup

    def fail_prepared(record):
        assert record["phase"] == "prepared"
        for credential in record["credentials"]:
            assert (state.root / "credentials" / f'{credential["credential_ref"]}.json').is_file()
        if kind == "oauth":
            assert list(state.oauth_staging_dir.glob("*.json"))
        raise OSError("fixture process loss before prepared journal replacement")

    monkeypatch.setattr(service.migration_journal, "save", fail_prepared)
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids, clean_api_keys=True))
    assert native.read_bytes() == original
    assert not store.config.sources
    assert service.migration_journal.load() is None
    assert service.revocations.list()
    assert list((state.root / "credentials").glob("*.json"))
    assert not list(state.auth_dir.glob("*.json"))
    recovered, _, recovered_adapter = _service(tmp_path, migration_home=tmp_path / "native")
    recovered.store = store
    recovered_adapter.revoke_credential = runtime.revoke_credential
    asyncio.run(recovered._ensure_engine_synced())
    assert not recovered.revocations.list()
    assert not list((state.root / "credentials").iterdir())
    if state.oauth_staging_dir.exists():
        assert not list(state.oauth_staging_dir.iterdir())
    assert not list(state.auth_dir.glob("*.json"))
    assert native.read_bytes() == original
