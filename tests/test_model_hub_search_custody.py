"""Search create custody outlives failed requests without requiring model-engine demand."""

import asyncio

import pytest

from core.handlers.model_hub.adapter import SearchCredentialCheck
from core.handlers.model_hub.service import ModelHubError
from tests.test_model_hub_api import _service
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.state import EngineStateStore


NONCE = "scn_searchcleanup0001"


def _draft(**changes):
    return {"kind": "api_key", "vendor": "tavily", "key": "test-search-key", "client_nonce": NONCE, **changes}


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("nonce", [NONCE, "scn_searchcleanup0002", None])
def test_search_create_replays_durable_cleanup_before_new_custody(tmp_path, monkeypatch, restart, nonce):
    service, store, adapter = _service(tmp_path)
    adapter.search_check_outcome = "invalid"

    async def unavailable(_ref):
        raise OSError("test credential store unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(adapter, "revoke_api_key_credential", unavailable)
        with pytest.raises(ModelHubError):
            asyncio.run(service.create_source(_draft()))
    assert len(service.revocations.list()) == 1
    assert NONCE not in service._source_create_nonces  # Durable cleanup releases the claim.
    assert not store.load().sources
    if restart:
        service, store, adapter = _service(tmp_path, adapter)
    operations = []
    provision = adapter.provision_credential
    revoke = adapter.revoke_api_key_credential

    async def record_provision(*args):
        operations.append("provision")
        return await provision(*args)

    async def record_revoke(ref):
        operations.append("revoke")
        return await revoke(ref)

    monkeypatch.setattr(adapter, "provision_credential", record_provision)
    monkeypatch.setattr(adapter, "revoke_api_key_credential", record_revoke)
    adapter.search_check_outcome = "pass"
    adapter.fail_sync = True  # No model engine dependency for a search-only retry.
    draft = _draft(client_nonce=nonce)
    if nonce is None:
        draft.pop("client_nonce")
    result = asyncio.run(service.create_source(draft))
    assert operations == ["revoke", "provision"]
    assert result["source"].get("client_nonce") == nonce
    assert service.revocations.list() == []
    assert adapter.synced == []
    assert adapter.start_calls == 0
    assert adapter.ensure_calls == []


def test_search_retry_removes_the_real_local_orphan_without_an_engine(tmp_path, monkeypatch):
    class NoEngine:
        def __getattr__(self, name):
            pytest.fail(f"search cleanup reached the model engine: {name}")

    custody = EngineStateStore(tmp_path / "custody")
    real = CLIProxyEngineAdapter(supervisor=NoEngine(), state_store=custody)
    old_ref = asyncio.run(real.provision_credential("tavily", "search", "test-old-key", None))
    service, store, adapter = _service(tmp_path)
    service.revocations.add("src_oldsearch01", old_ref, operation="revoke_api_key_credential")
    monkeypatch.setattr(adapter, "provision_credential", real.provision_credential)
    monkeypatch.setattr(adapter, "revoke_api_key_credential", real.revoke_api_key_credential)
    created = asyncio.run(service.create_source(_draft()))["source"]
    assert custody.credential_metadata_if_present(old_ref) is None
    assert custody.credential_metadata_if_present(created["credential_ref"]) is not None
    assert service.revocations.list() == []
    assert store.load().sources[0].credential_ref == created["credential_ref"]
    assert adapter.synced == []


def test_search_cleanup_replay_preserves_live_and_takeover_credentials(tmp_path, monkeypatch):
    service, store, adapter = _service(tmp_path)
    source = asyncio.run(service.create_source(_draft()))["source"]
    service.revocations.add(source["id"], source["credential_ref"], operation="revoke_api_key_credential")
    service.revocations.add("src_takeover01", "cred_takeover01", operation="revoke_api_key_credential")
    service.revocations.add("src_oauthold01", "cred_oauthold01")
    monkeypatch.setattr(service.migration_journal, "load", lambda: {
        "credentials": [{"source_id": "src_takeover01", "credential_ref": "cred_takeover01"}],
    })
    async def replay():
        async with service._mutation_lock:
            await service._replay_credential_revocations_locked(
                store.load(),
                [row for row in service.revocations.list() if row.operation == "revoke_api_key_credential"],
            )

    asyncio.run(replay())
    assert not adapter.revoked
    assert {entry.credential_ref for entry in service.revocations.list()} == {"cred_takeover01", "cred_oauthold01"}
    assert store.load().sources[0].credential_ref == source["credential_ref"]
    assert adapter.synced == []


@pytest.mark.parametrize("outcome", ["invalid", "cancelled"])
def test_deepseek_create_free_check_is_inside_permanent_custody_rollback(tmp_path, monkeypatch, outcome):
    service, store, adapter = _service(tmp_path)

    async def check(_ref, _catalog):
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return SearchCredentialCheck("invalid")

    monkeypatch.setattr(adapter, "check_search_credential", check)
    with pytest.raises(asyncio.CancelledError if outcome == "cancelled" else ModelHubError):
        asyncio.run(service.create_source(_draft(vendor="deepseek", save_unverified=True)))
    assert store.load().sources == []
    assert adapter.revoked == ["cred_test001"]
    assert service.revocations.list() == []
    assert NONCE not in service._source_create_nonces


def test_cancelled_search_provision_journals_typed_cleanup_and_can_retry(tmp_path, monkeypatch):
    async def scenario():
        service, store, adapter = _service(tmp_path)
        provision = adapter.provision_credential
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_provision(*args):
            ref = await provision(*args)
            entered.set()
            await release.wait()
            return ref

        async def fail_revoke(_ref):
            raise OSError("test revocation unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(adapter, "provision_credential", blocked_provision)
            patch.setattr(adapter, "revoke_api_key_credential", fail_revoke)
            task = asyncio.create_task(service.create_source(_draft()))
            await entered.wait()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not store.load().sources
        assert NONCE not in service._source_create_nonces
        assert [(row.credential_ref, row.operation) for row in service.revocations.list()] == [
            ("cred_test001", "revoke_api_key_credential"),
        ]
        await service.create_source(_draft())
        assert service.revocations.list() == []
        assert adapter.revoked == ["cred_test001"]
        assert adapter.synced == []

    asyncio.run(scenario())


def test_search_create_drains_cleanup_despite_repeated_cancellation(tmp_path, monkeypatch):
    async def scenario():
        service, store, adapter = _service(tmp_path)
        checking, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        revoke = adapter.revoke_api_key_credential

        async def check(*_args):
            checking.set()
            await asyncio.Event().wait()

        async def blocked_revoke(ref):
            cleaning.set()
            await release.wait()
            await revoke(ref)

        monkeypatch.setattr(adapter, "check_search_credential", check)
        monkeypatch.setattr(adapter, "revoke_api_key_credential", blocked_revoke)
        task = asyncio.create_task(service.create_source(_draft()))
        await checking.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert NONCE in service._source_create_nonces
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert NONCE not in service._source_create_nonces
        assert adapter.revoked == ["cred_test001"]
        assert service.revocations.list() == []
        assert not store.load().sources

    asyncio.run(scenario())


def test_search_create_cancellation_after_commit_keeps_the_bound_credential(tmp_path, monkeypatch):
    service, store, adapter = _service(tmp_path)
    commit = service._commit_new_source_locked

    async def interrupted_commit(*args, **kwargs):
        await commit(*args, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "_commit_new_source_locked", interrupted_commit)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.create_source(_draft()))
    assert store.load().sources[0].credential_ref == "cred_test001"
    assert adapter.revoked == []
    assert NONCE not in service._source_create_nonces
    with pytest.raises(ModelHubError) as duplicate:
        asyncio.run(service.create_source(_draft()))
    assert duplicate.value.code == "source_nonce_conflict"
    assert adapter.credential_count == 1


@pytest.mark.parametrize("stage", ["invalid", "cancelled", "commit"])
def test_search_create_retains_nonce_only_when_cleanup_is_not_durable(tmp_path, monkeypatch, stage):
    service, store, adapter = _service(tmp_path)
    adapter.search_check_outcome = "invalid" if stage == "invalid" else "pass"

    async def fail_revoke(_ref):
        raise OSError("test revocation unavailable")

    def fail_journal(*_args, **_kwargs):
        raise OSError("test journal unavailable")

    async def cancel_check(*_args):
        raise asyncio.CancelledError

    async def fail_commit(*_args, **_kwargs):
        raise ModelHubError("engine_down")

    monkeypatch.setattr(adapter, "revoke_api_key_credential", fail_revoke)
    monkeypatch.setattr(service.revocations, "add", fail_journal)
    if stage == "cancelled":
        monkeypatch.setattr(adapter, "check_search_credential", cancel_check)
    if stage == "commit":
        monkeypatch.setattr(service, "_commit_new_source_locked", fail_commit)
    with pytest.raises(ModelHubError) as failed:
        asyncio.run(service.create_source(_draft()))
    assert failed.value.code == "engine_down"
    assert NONCE in service._source_create_nonces
    before = adapter.credential_count
    with pytest.raises(ModelHubError) as blocked:
        asyncio.run(service.create_source(_draft()))
    assert blocked.value.code == "source_create_in_progress"
    assert adapter.credential_count == before
    assert not store.load().sources
