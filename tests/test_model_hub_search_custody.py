"""Search create custody outlives failed requests without requiring model-engine demand."""

import asyncio

import pytest

from core.handlers.model_hub.adapter import SearchCredentialCheck
from core.handlers.model_hub.service import ModelHubError
from tests.test_model_hub_api import _service
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore


NONCE = "scn_searchcleanup0001"


class _NoModelEngine:
    def __getattr__(self, name):
        pytest.fail(f"search custody reached the model engine: {name}")


def _draft(**changes):
    return {"kind": "api_key", "vendor": "tavily", "key": "test-search-key", "client_nonce": NONCE, **changes}


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("nonce", [NONCE, "scn_searchcleanup0002", None])
@pytest.mark.parametrize("failure", ["revoke", "journal_remove"])
def test_search_create_waits_for_durable_cleanup_before_new_custody(tmp_path, monkeypatch, restart, nonce, failure):
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
    pending = service.revocations.list()
    before = adapter.credential_count
    with monkeypatch.context() as patch:
        if failure == "revoke":
            patch.setattr(adapter, "revoke_api_key_credential", unavailable)
        else:
            def fail_remove(*_args):
                raise OSError("test journal removal unavailable")
            patch.setattr(service.revocations, "remove", fail_remove)
        for _ in range(2):
            with pytest.raises(ModelHubError) as blocked:
                asyncio.run(service.create_source(draft))
            assert (blocked.value.code, blocked.value.status) == ("engine_down", 503)
            assert adapter.credential_count == before
            assert service.revocations.list() == pending
            assert not service._source_create_nonces
            assert not store.load().sources
    operations.clear()
    result = asyncio.run(service.create_source(draft))
    assert operations == ["revoke", "provision"]
    assert result["source"].get("client_nonce") == nonce
    assert service.revocations.list() == []
    assert adapter.synced == []
    assert adapter.start_calls == 0
    assert adapter.ensure_calls == []


def test_search_retry_removes_the_real_local_orphan_without_an_engine(tmp_path, monkeypatch):
    custody = EngineStateStore(tmp_path / "custody")
    real = CLIProxyEngineAdapter(supervisor=_NoModelEngine(), state_store=custody)
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


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("failure", ["journal_remove", "after_unlink"])
def test_search_cleanup_replay_keeps_its_identity_after_unlink(tmp_path, monkeypatch, restart, failure):
    custody = EngineStateStore(tmp_path / "custody")
    real = CLIProxyEngineAdapter(supervisor=_NoModelEngine(), state_store=custody)
    service, store, adapter = _service(tmp_path)
    monkeypatch.setattr(adapter, "provision_credential", real.provision_credential)
    monkeypatch.setattr(adapter, "revoke_api_key_credential", real.revoke_api_key_credential)
    monkeypatch.setattr(adapter, "revoke_search_credential", real.revoke_search_credential, raising=False)
    adapter.search_check_outcome = "invalid"
    retire = custody.revoke_api_key_credential

    def fail_remove(*_args):
        raise OSError("test journal removal unavailable")

    def fail_after_unlink(ref):
        retire(ref)
        raise OSError("test directory flush failed after unlink")

    with monkeypatch.context() as patch:
        if failure == "journal_remove":
            patch.setattr(service.revocations, "remove", fail_remove)
        else:
            patch.setattr(custody, "revoke_api_key_credential", fail_after_unlink)
        with pytest.raises(ModelHubError) as failed:
            asyncio.run(service.create_source(_draft()))
        assert failed.value.detail == "modelHub.errors.authentication_failed"
    pending = service.revocations.list()
    assert len(pending) == 1
    assert pending[0].operation == "revoke_search_credential"
    assert custody.credential_metadata_if_present(pending[0].credential_ref) is None
    assert not store.load().sources
    if restart:
        service, store, adapter = _service(tmp_path, adapter)
    adapter.search_check_outcome = "pass"
    created = asyncio.run(service.create_source(_draft()))["source"]
    assert service.revocations.list() == []
    assert custody.credential_metadata_if_present(created["credential_ref"]) is not None
    assert store.load().sources[0].credential_ref == created["credential_ref"]
    assert adapter.synced == []


@pytest.mark.parametrize("document", ["missing", "corrupt", "model"])
def test_search_retirement_uses_durable_intent_but_rejects_readable_model_identity(tmp_path, document):
    custody = EngineStateStore(tmp_path / "custody")
    adapter = CLIProxyEngineAdapter(supervisor=_NoModelEngine(), state_store=custody)
    vendor, protocol = ("anthropic", "anthropic") if document == "model" else ("tavily", "search")
    ref = asyncio.run(adapter.provision_credential(vendor, protocol, "test-only-key", None))
    if document == "model":
        with pytest.raises(EngineStateError, match="not a search API key"):
            asyncio.run(adapter.revoke_search_credential(ref))
        assert custody.credential_metadata_if_present(ref) is not None
        return
    if document == "missing":
        custody.revoke_api_key_credential(ref)
    else:
        (custody.root / "credentials" / f"{ref}.json").write_text("{broken", encoding="utf-8")
    asyncio.run(adapter.revoke_search_credential(ref))
    asyncio.run(adapter.revoke_search_credential(ref))
    assert custody.credential_metadata_if_present(ref) is None


@pytest.mark.parametrize("inflight", [False, True])
@pytest.mark.parametrize(("mutation", "failure"), [
    ("replace", "provision"), ("replace", "cancel_provision"),
    ("replace", "commit"), ("delete", "commit"),
    ("replace", "cancel_before_commit"), ("delete", "cancel_before_commit"),
    ("replace", "cancel_after_commit"), ("delete", "cancel_after_commit"),
    ("replace", None), ("delete", None),
])
def test_search_trial_retirement_follows_mutation_commit(tmp_path, monkeypatch, inflight, mutation, failure):
    from config.v2_config import ModelHubSourceStateConfig

    service, store, adapter = _service(tmp_path)
    created = asyncio.run(service.create_source(_draft(vendor="exa")))["source"]
    source_id = created["id"]
    store.config.sources[0].state = ModelHubSourceStateConfig(
        status="needs_action", detail_key="models.source.needs_action.spend_limit_reached",
    )
    asyncio.run(service.reenable_search_source(source_id))
    if inflight:
        assert service.reserve_search_reenable_trial(source_id)
    before = store.load().to_payload()
    commit = service._commit_synced

    async def failed_provision(*_args):
        if failure == "cancel_provision":
            raise asyncio.CancelledError
        raise ModelHubError("engine_down", status=503)

    async def failed_commit(*args, **kwargs):
        if failure == "cancel_after_commit":
            await commit(*args, **kwargs)
        if failure.startswith("cancel_"):
            raise asyncio.CancelledError
        raise ModelHubError("engine_down", status=503)

    if failure in {"provision", "cancel_provision"}:
        monkeypatch.setattr(adapter, "provision_credential", failed_provision)
    elif failure is not None:
        monkeypatch.setattr(service, "_commit_synced", failed_commit)

    async def mutate():
        if mutation == "replace":
            return await service.replace_credential(source_id, {"key": "test-replacement-key"})
        return await service.delete_source(source_id)

    if failure is None:
        asyncio.run(mutate())
    else:
        with pytest.raises(asyncio.CancelledError if failure.startswith("cancel_") else ModelHubError):
            asyncio.run(mutate())
    committed = failure in {None, "cancel_after_commit"}
    assert service.search_reenable_trial_pending(source_id) is not committed
    assert service.search_reenable_trial_armed(source_id) is (not committed and not inflight)
    if committed:
        assert store.load().to_payload() != before
    else:
        assert store.load().to_payload() == before


@pytest.mark.parametrize("remove_fails", [False, True])
@pytest.mark.parametrize("operation", ["revoke_api_key_credential", "revoke_search_credential"])
def test_search_cleanup_admission_preserves_live_and_takeover_credentials(
    tmp_path, monkeypatch, remove_fails, operation,
):
    service, store, adapter = _service(tmp_path)
    source = asyncio.run(service.create_source(_draft()))["source"]
    service.revocations.add(source["id"], source["credential_ref"], operation=operation)
    service.revocations.add("src_takeover01", "cred_takeover01", operation=operation)
    service.revocations.add("src_oauthold01", "cred_oauthold01")
    service.revocations.add("src_oauthorphan01", "cred_oauthorphan01", operation="cleanup_orphaned_oauth_material")
    monkeypatch.setattr(service.migration_journal, "load", lambda: {
        "credentials": [{"source_id": "src_takeover01", "credential_ref": "cred_takeover01"}],
    })
    if remove_fails:
        def fail_remove(*_args):
            raise OSError("test active-intent removal unavailable")
        monkeypatch.setattr(service.revocations, "remove", fail_remove)
    async def admit():
        async with service._mutation_lock:
            await service._require_api_key_cleanup_locked(store.load())

    asyncio.run(admit())
    assert not adapter.revoked
    assert not adapter.orphan_cleanup_calls
    expected = {"cred_takeover01", "cred_oauthold01", "cred_oauthorphan01"}
    if remove_fails:
        expected.add(source["credential_ref"])
    assert {entry.credential_ref for entry in service.revocations.list()} == expected
    assert store.load().sources[0].credential_ref == source["credential_ref"]
    assert adapter.synced == []


@pytest.mark.parametrize("outcome", ["pass", "invalid"])
@pytest.mark.parametrize("next_mutation", ["create", "replace"])
def test_search_replacement_cleanup_gates_subsequent_custody(tmp_path, monkeypatch, outcome, next_mutation):
    service, store, adapter = _service(tmp_path)
    source = asyncio.run(service.create_source(_draft()))["source"]

    async def unavailable(_ref):
        raise OSError("test retirement unavailable")

    async def mutate():
        if next_mutation == "replace":
            return await service.replace_credential(source["id"], {"key": "test-next-key"})
        return await service.create_source(_draft(client_nonce="scn_searchcleanup0002"))

    with monkeypatch.context() as patch:
        patch.setattr(adapter, "revoke_api_key_credential", unavailable)
        adapter.search_check_outcome = outcome
        if outcome == "invalid":
            with pytest.raises(ModelHubError):
                asyncio.run(service.replace_credential(source["id"], {"key": "test-replacement-key"}))
        else:
            asyncio.run(service.replace_credential(source["id"], {"key": "test-replacement-key"}))
        pending = service.revocations.list()
        assert len(pending) == 1
        assert pending[0].operation == "revoke_search_credential"
        assert pending[0].credential_ref == (source["credential_ref"] if outcome == "pass" else "cred_test002")
        before_config, before_count = store.load().to_payload(), adapter.credential_count
        adapter.search_check_outcome = "pass"
        for _ in range(2):
            with pytest.raises(ModelHubError) as blocked:
                asyncio.run(mutate())
            assert (blocked.value.code, blocked.value.status) == ("engine_down", 503)
            assert adapter.credential_count == before_count
            assert store.load().to_payload() == before_config
            assert service.revocations.list() == pending
    asyncio.run(mutate())
    assert service.revocations.list() == []
    assert adapter.credential_count == before_count + 1
    assert adapter.synced == []


@pytest.mark.parametrize("failure", ["unreadable", "cancelled"])
def test_search_cleanup_admission_failure_releases_nonce_without_provisioning(tmp_path, monkeypatch, failure):
    service, store, adapter = _service(tmp_path)
    service.revocations.add("src_orphan01", "cred_orphan01", operation="revoke_api_key_credential")
    pending = service.revocations.list()

    def unreadable():
        raise OSError("test journal unreadable")

    async def cancelled(_ref):
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        if failure == "unreadable":
            patch.setattr(service.revocations, "list", unreadable)
        else:
            patch.setattr(adapter, "revoke_api_key_credential", cancelled)
        with pytest.raises(ModelHubError if failure == "unreadable" else asyncio.CancelledError) as blocked:
            asyncio.run(service.create_source(_draft()))
        if failure == "unreadable":
            assert (blocked.value.code, blocked.value.status) == ("engine_down", 503)
    assert not store.load().sources
    assert not service._source_create_nonces
    assert adapter.credential_count == 0
    assert service.revocations.list() == pending
    asyncio.run(service.create_source(_draft()))
    assert service.revocations.list() == []
    assert adapter.credential_count == 1
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
            ("cred_test001", "revoke_search_credential"),
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
