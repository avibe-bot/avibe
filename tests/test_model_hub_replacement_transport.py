"""Replacement recovers transport, never infers absence from unreadable custody."""

import asyncio
import json
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from config.v2_config import ModelHubSourceConfig
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import ModelHubError
from tests.test_model_hub_migration_bearer import (
    CUSTOM,
    KEY,
    _adapter,
    _binding,
    _relay,
    _service_with_custody,
)
from vibe.model_hub_runtime.config import write_engine_config
from vibe.model_hub_runtime import state as state_module
from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore, RuntimeSecrets


def _document(store, ref):
    return store.root / "credentials" / f"{ref}.json"


def _damage(store, ref, damage):
    path = _document(store, ref)
    payload = json.loads(path.read_text())
    if damage == "missing":
        path.unlink()
    elif damage == "bad_json":
        path.write_text("{")
    elif damage == "non_object":
        path.write_text("[]")
    elif damage == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif damage == "missing_value":
        payload.pop("value")
        path.write_text(json.dumps(payload))
    elif damage == "empty_value":
        payload["value"] = ""
        path.write_text(json.dumps(payload))
    elif damage == "invalid_value":
        payload["value"] = []
        path.write_text(json.dumps(payload))
    elif damage == "wrongkind":
        payload.update(kind="oauth", auth_name="unrelated.json")
        path.write_text(json.dumps(payload))
    elif damage == "unknown_kind":
        payload["kind"] = "future-kind"
        path.write_text(json.dumps(payload))
    elif damage == "unknown_scheme":
        payload["auth_scheme"] = "future-scheme"
        path.write_text(json.dumps(payload))
    elif damage == "missing_scheme":
        payload.pop("auth_scheme")
        path.write_text(json.dumps(payload))
    elif damage == "null_scheme":
        payload["auth_scheme"] = None
        path.write_text(json.dumps(payload))
    elif damage == "permissions":
        path.chmod(0o644)
    elif damage == "directory_permissions":
        path.parent.chmod(0o755)
    elif damage == "symlink":
        target = path.with_name("fixture-target.json")
        path.rename(target)
        path.symlink_to(target)
    else:
        raise AssertionError(damage)


async def _source(tmp_path, origin, scheme, *, plain_bearer=False):
    service, config, runtime = _service_with_custody(tmp_path)
    ref = await runtime.provision_credential(
        "anthropic", "anthropic", KEY, origin, auth_scheme=scheme,
    )
    if plain_bearer:
        old_ref = ref
        ref = "cred_" + "b" * 32
        _document(runtime.state_store, old_ref).rename(_document(runtime.state_store, ref))
    source = ModelHubSourceConfig.from_payload({
        "id": "src_bearerfixture", "kind": "api_key", "vendor": "anthropic",
        "display_name": "Fixture", "protocol": "anthropic", "base_url": origin,
        "supply_channel": "hub", "billing": "metered",
        "state": {"status": "standby"}, "models": [], "credential_ref": ref,
    })
    config.config.sources.append(source)
    runtime.state_store.sync_sources([_binding(ref, origin)])
    return service, config, runtime, source, ref


def test_new_bearer_namespace_is_immutable_and_reserved_before_secret(tmp_path):
    store = EngineStateStore(tmp_path / "engine")
    reserved = []

    def on_reserved(ref):
        assert re.fullmatch(r"cred_auth_bearer_[0-9a-f]{32}", ref)
        assert len(store._credential_namespace_paths(ref)) == 4
        for path in store._credential_namespace_paths(ref):
            assert json.loads(path.read_text()) == {"kind": "reservation", "credential_ref": ref}
        reserved.append(ref)

    ref = store.store_api_key(
        KEY, vendor="anthropic", protocol="anthropic", base_url=CUSTOM,
        auth_scheme="bearer", on_reserved=on_reserved,
    )
    assert reserved == [ref]
    legacy = store.store_api_key(KEY)
    assert re.fullmatch(r"cred_[0-9a-f]{32}", legacy)


@pytest.mark.parametrize("scheme", [None, "bearer"])
@pytest.mark.parametrize("damage", [
    "missing", "bad_json", "non_object", "invalid_utf8",
    "missing_value", "empty_value", "invalid_value",
])
def test_replacement_recovers_without_old_secret_and_retires_namespace(tmp_path, scheme, damage):
    async def scenario():
        async with _relay(scheme=scheme or "legacy") as (origin, requests, accepted):
            service, config, runtime, source, ref = await _source(tmp_path, origin, scheme)
            _damage(runtime.state_store, ref, damage)
            accepted.add("fixture-replacement")
            result = await service.replace_credential(source.id, {"key": "fixture-replacement"})
            new_ref = result["source"]["credential_ref"]
            assert new_ref != ref
            assert await runtime.credential_auth_scheme(new_ref) == scheme
            assert config.config.sources[0].credential_ref == new_ref
            assert "auth_scheme" not in result["source"]
            assert requests and requests[0][2].get("Authorization" if scheme else "x-api-key") == (
                "Bearer fixture-replacement" if scheme else "fixture-replacement"
            )
            assert not service.revocations.list()
            assert all(not path.exists() for path in runtime.state_store._credential_namespace_paths(ref))

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", [
    "wrongkind", "unknown_kind", "unknown_scheme", "missing_scheme", "null_scheme",
    "permissions", "directory_permissions", "symlink",
])
def test_replacement_refuses_uncertain_identity_before_new_secret_or_request(tmp_path, damage):
    async def scenario():
        async with _relay() as (origin, requests, _):
            service, config, runtime, source, ref = await _source(tmp_path, origin, "bearer")
            _damage(runtime.state_store, ref, damage)
            before = config.config.to_payload()
            service.adapter.provision_credential = AsyncMock(side_effect=AssertionError("must not provision"))
            with pytest.raises(ModelHubError):
                await service.replace_credential(source.id, {"key": "fixture-replacement"})
            assert config.config.to_payload() == before
            assert not requests
            service.adapter.provision_credential.assert_not_awaited()
            assert not service.revocations.list()
            if damage == "directory_permissions":
                assert _document(runtime.state_store, ref).parent.stat().st_mode & 0o777 == 0o755

    asyncio.run(scenario())


@pytest.mark.parametrize("ref", [
    "cred_auth_future_" + "a" * 32, "cred_auth_bearer_abcdef",
    "cred_auth_bearer_" + "A" * 32, "cred_auth_", "../cred_fixture",
])
def test_reserved_unknown_or_malformed_refs_never_recover_as_legacy(tmp_path, ref):
    async def scenario():
        adapter = _adapter(tmp_path)
        with pytest.raises(EngineStateError):
            await adapter.credential_auth_scheme(ref)
        with pytest.raises(EngineStateError):
            await adapter.revoke_api_key_credential(ref)
        assert not adapter.state_store.root.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["replace", "retarget"])
def test_intact_unpublished_plain_bearer_upgrades_only_in_fresh_ref(tmp_path, operation):
    async def scenario():
        async with _relay() as (origin, requests, accepted):
            service, _, runtime, source, ref = await _source(
                tmp_path, origin, "bearer", plain_bearer=True,
            )
            assert await runtime.credential_auth_scheme(ref) == "bearer"
            old = _document(runtime.state_store, ref).read_bytes()
            if operation == "replace":
                accepted.add("fixture-replacement")
                result = await service.replace_credential(source.id, {"key": "fixture-replacement"})
                new_ref = result["source"]["credential_ref"]
                assert requests
            else:
                new_ref = await runtime.retarget_api_key_credential(ref, "anthropic", "anthropic", origin)
                assert _document(runtime.state_store, ref).read_bytes() == old
            assert re.fullmatch(r"cred_auth_bearer_[0-9a-f]{32}", new_ref)

    asyncio.run(scenario())


@pytest.mark.parametrize("consumer", [
    "metadata", "read_key", "reuse", "retarget", "sync", "config", "discover",
    "observe", "refresh", "oauth_inventory", "ready",
])
def test_real_consumers_cannot_downgrade_tagged_metadata(tmp_path, consumer):
    async def scenario():
        async with _relay() as (origin, requests, _):
            runtime = _adapter(tmp_path)
            ref = await runtime.provision_credential(
                "anthropic", "anthropic", KEY, origin, auth_scheme="bearer",
            )
            store = runtime.state_store
            binding = _binding(ref, origin)
            record = store.sync_sources([binding])[0]
            _damage(store, ref, "wrongkind" if consumer == "oauth_inventory" else "missing_scheme")
            if consumer == "ready":
                assert not store.has_current_source_credential(
                    ref, source_id=binding.source_id, kind="api_key", vendor="anthropic",
                    protocol="anthropic", base_url=origin,
                )
            else:
                with pytest.raises(EngineStateError):
                    if consumer == "metadata":
                        store.credential_metadata(ref)
                    elif consumer == "read_key":
                        store.read_api_key(ref)
                    elif consumer == "reuse":
                        await runtime.matches_api_key_credential(ref, "anthropic", "anthropic", KEY, origin)
                    elif consumer == "retarget":
                        await runtime.retarget_api_key_credential(ref, "anthropic", "anthropic", origin)
                    elif consumer == "sync":
                        store.sync_sources([binding])
                    elif consumer == "config":
                        write_engine_config(
                            tmp_path / "config.yaml", host="127.0.0.1", port=23456,
                            auth_dir=store.auth_dir, state_store=store,
                            runtime_secrets=RuntimeSecrets("fixture-management", "fixture-gateway"),
                            sources=[record],
                        )
                    elif consumer == "discover":
                        await runtime.discover_models("anthropic", "anthropic", origin, ref)
                    elif consumer == "observe":
                        await runtime.observe_source("anthropic", origin, ref, ("anthropic",))
                    elif consumer == "refresh":
                        await runtime.credential_supports_refresh(ref)
                    elif consumer == "oauth_inventory":
                        store.oauth_credential_ref("unrelated.json")
            assert not requests

    asyncio.run(scenario())


@pytest.mark.parametrize("scheme", [None, "bearer"])
def test_recovered_scheme_does_not_bypass_new_key_authentication(tmp_path, scheme):
    async def scenario():
        async with _relay(scheme=scheme or "legacy") as (origin, requests, _):
            service, config, runtime, source, ref = await _source(tmp_path, origin, scheme)
            _damage(runtime.state_store, ref, "bad_json")
            with pytest.raises(ModelHubError):
                await service.replace_credential(source.id, {"key": "fixture-wrong-key"})
            assert requests
            assert config.config.sources[0].credential_ref == ref
            assert _document(runtime.state_store, ref).read_text() == "{"
            assert list((runtime.state_store.root / "credentials").glob("cred_*.json")) == [
                _document(runtime.state_store, ref)
            ]
            assert not service.revocations.list()

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["missing", "bad_json", "missing_value"])
def test_typed_retirement_only_removes_unbound_exact_namespace(tmp_path, damage):
    async def scenario():
        runtime = _adapter(tmp_path)
        store = runtime.state_store
        ref = await runtime.provision_credential("anthropic", "anthropic", KEY, CUSTOM, auth_scheme="bearer")
        store.sync_sources([_binding(ref)])
        _damage(store, ref, damage)
        with pytest.raises(EngineStateError, match="still bound"):
            await runtime.revoke_api_key_credential(ref)
        store.replace_sources([])
        store.audit_auth_permissions()
        auth = store.auth_dir / "unrelated.json"
        store._secure_write_json(auth, {"fixture": "must survive"})
        other = await runtime.provision_credential("anthropic", "anthropic", KEY, CUSTOM)
        paths = store._credential_namespace_paths(ref)
        for path in paths[1:]:
            store._secure_write_json(path, {"fixture": "owned interrupted temp", "auth_name": auth.name})
        runtime.supervisor.client_if_running = lambda: pytest.fail("API cleanup must not inspect OAuth engine")
        await runtime.revoke_api_key_credential(ref)
        await runtime.revoke_api_key_credential(ref)
        assert all(not path.exists() for path in paths)
        assert auth.exists()
        assert store.read_api_key(other) == KEY

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["wrongkind", "unknown_kind", "unknown_scheme", "missing_scheme", "permissions", "symlink"])
def test_replay_failure_is_not_false_absence_and_retains_durable_intent(tmp_path, damage):
    async def scenario():
        service, _, runtime, _, ref = await _source(tmp_path, CUSTOM, "bearer")
        runtime.state_store.replace_sources([])
        service.store.config.sources.clear()
        _damage(runtime.state_store, ref, damage)
        service.revocations.add("src_bearerfixture", ref, operation="revoke_api_key_credential")
        service.revocations = CredentialRevocationJournal(service.revocations.path)
        service._engine_synced = False
        await service._ensure_engine_synced()
        assert len(service.revocations.list()) == 1
        assert _document(runtime.state_store, ref).exists()

    asyncio.run(scenario())


def test_postcommit_retirement_failure_replays_typed_intent_after_restart(tmp_path):
    async def scenario():
        async with _relay() as (origin, _, accepted):
            service, config, runtime, source, ref = await _source(tmp_path, origin, "bearer")
            _damage(runtime.state_store, ref, "bad_json")
            accepted.add("fixture-replacement")
            service.adapter.revoke_api_key_credential = AsyncMock(side_effect=OSError("fixture I/O"))
            answer = await service.replace_credential(source.id, {"key": "fixture-replacement"})
            assert config.config.sources[0].credential_ref == answer["source"]["credential_ref"]
            pending = CredentialRevocationJournal(service.revocations.path).list()
            assert [(entry.credential_ref, entry.operation) for entry in pending] == [
                (ref, "revoke_api_key_credential"),
            ]
            restarted, restored, reopened_runtime = _service_with_custody(tmp_path)
            restored.save(type(config.config).from_payload(config.config.to_payload()))
            restarted.adapter.revoke_credential = AsyncMock(side_effect=AssertionError("must use typed operation"))
            await restarted._ensure_engine_synced()
            assert not restarted.revocations.list()
            assert not _document(reopened_runtime.state_store, ref).exists()
            assert await reopened_runtime.credential_auth_scheme(answer["source"]["credential_ref"]) == "bearer"

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [PermissionError("fixture denied"), OSError("fixture I/O")])
def test_real_read_error_is_not_content_corruption_or_retirement_absence(tmp_path, monkeypatch, error):
    async def scenario():
        async with _relay() as (origin, requests, _):
            service, config, runtime, source, ref = await _source(tmp_path, origin, "bearer")
            path = _document(runtime.state_store, ref)
            original_open = os.open

            def fail_read(target, flags, *args, **kwargs):
                if Path(target) == path:
                    raise error
                return original_open(target, flags, *args, **kwargs)

            monkeypatch.setattr(state_module.os, "open", fail_read)
            service.adapter.provision_credential = AsyncMock(side_effect=AssertionError("must not provision"))
            with pytest.raises(ModelHubError):
                await service.replace_credential(source.id, {"key": "fixture-replacement"})
            assert not requests
            service.adapter.provision_credential.assert_not_awaited()
            config.config.sources.clear()
            runtime.state_store.replace_sources([])
            service.revocations.add(source.id, ref, operation="revoke_api_key_credential")
            await service._ensure_engine_synced()
            assert len(service.revocations.list()) == 1
            assert path.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("unsafe", ["symlink", "permissions", "directory"])
def test_retirement_preflights_all_exact_paths_and_never_touches_oauth(tmp_path, unsafe):
    async def scenario():
        runtime = _adapter(tmp_path)
        store = runtime.state_store
        ref = await runtime.provision_credential("anthropic", "anthropic", KEY, CUSTOM, auth_scheme="bearer")
        _damage(store, ref, "bad_json")
        paths = store._credential_namespace_paths(ref)
        store.audit_auth_permissions()
        auth = store.auth_dir / "unrelated.json"
        store._secure_write_json(auth, {"fixture": "keep"})
        before = auth.read_bytes()
        if unsafe == "symlink":
            paths[1].symlink_to(auth)
        elif unsafe == "permissions":
            store._secure_write_json(paths[1], {"fixture": "unsafe"})
            paths[1].chmod(0o644)
        else:
            paths[1].mkdir(mode=0o700)
        store._secure_write_json(paths[2], {"fixture": "also keep until safe"})
        with pytest.raises(EngineStateError):
            await runtime.revoke_api_key_credential(ref)
        assert _document(store, ref).read_text() == "{"
        assert paths[2].exists()
        assert auth.read_bytes() == before

    asyncio.run(scenario())


def test_unlink_then_failed_fsync_keeps_intent_until_absence_is_durable(tmp_path, monkeypatch):
    async def scenario():
        async with _relay() as (origin, _, accepted):
            service, config, runtime, source, ref = await _source(tmp_path, origin, "bearer")
            accepted.add("fixture-replacement")
            path = _document(runtime.state_store, ref)
            real_fsync = EngineStateStore._fsync_directory
            fail = True
            fences = []

            def fsync(directory):
                if directory == path.parent and not path.exists():
                    fences.append(directory)
                    if fail:
                        raise OSError("fixture lost directory flush")
                real_fsync(directory)

            monkeypatch.setattr(EngineStateStore, "_fsync_directory", staticmethod(fsync))
            answer = await service.replace_credential(source.id, {"key": "fixture-replacement"})
            assert config.config.sources[0].credential_ref == answer["source"]["credential_ref"]
            assert not path.exists()
            assert len(service.revocations.list()) == 1
            assert fences
            service.revocations = CredentialRevocationJournal(service.revocations.path)
            service._engine_synced = False
            await service._ensure_engine_synced()
            assert len(service.revocations.list()) == 1
            fail = False
            count = len(fences)
            await service._ensure_engine_synced()
            assert len(fences) > count
            assert not service.revocations.list()

    asyncio.run(scenario())


def test_retirement_intent_is_durable_before_source_commit_and_preserved_on_crash(tmp_path, monkeypatch):
    async def scenario():
        async with _relay() as (origin, _, accepted):
            service, config, runtime, source, ref = await _source(tmp_path, origin, "bearer")
            accepted.add("fixture-replacement")
            save = config.save
            calls = []

            def assert_intent_before_save(value):
                if value.sources[0].credential_ref != ref:
                    pending = CredentialRevocationJournal(service.revocations.path).list()
                    assert [(row.credential_ref, row.operation) for row in pending] == [
                        (ref, "revoke_api_key_credential"),
                    ]
                    calls.append(value.sources[0].credential_ref)
                save(value)

            monkeypatch.setattr(config, "save", assert_intent_before_save)
            service.adapter.revoke_api_key_credential = AsyncMock(side_effect=OSError("fixture crash"))
            await service.replace_credential(source.id, {"key": "fixture-replacement"})
            assert calls and calls[-1] == config.config.sources[0].credential_ref
            assert _document(runtime.state_store, ref).exists()
            assert len(CredentialRevocationJournal(service.revocations.path).list()) == 1

    asyncio.run(scenario())


def test_failed_retirement_journal_fence_does_not_commit_or_delete_old_ref(tmp_path, monkeypatch):
    async def scenario():
        async with _relay() as (origin, _, accepted):
            service, config, runtime, source, ref = await _source(tmp_path, origin, "bearer")
            accepted.add("fixture-replacement")

            def fail_fence():
                raise OSError("fixture journal flush failed")

            monkeypatch.setattr(service.revocations, "_sync_directory", fail_fence)
            with pytest.raises(OSError, match="fixture journal flush failed"):
                await service.replace_credential(source.id, {"key": "fixture-replacement"})
            assert config.config.sources[0].credential_ref == ref
            assert runtime.state_store.read_api_key(ref) == KEY
            assert {path.stem for path in (runtime.state_store.root / "credentials").glob("cred_*.json")} == {ref}
            # A written-but-unflushed old-ref intent cannot retire the still
            # active credential. Restart reconciliation removes that stale row.
            await service._ensure_engine_synced()
            assert not service.revocations.list()
            assert runtime.state_store.read_api_key(ref) == KEY

    asyncio.run(scenario())


def test_replacement_can_recover_with_active_preupgrade_generic_intent(tmp_path):
    async def scenario():
        async with _relay() as (origin, _, accepted):
            service, config, runtime, source, ref = await _source(tmp_path, origin, "bearer")
            _damage(runtime.state_store, ref, "bad_json")
            service.revocations.add(source.id, ref)
            accepted.add("fixture-replacement")
            answer = await service.replace_credential(source.id, {"key": "fixture-replacement"})
            assert config.config.sources[0].credential_ref == answer["source"]["credential_ref"]
            assert not service.revocations.list()
            assert not _document(runtime.state_store, ref).exists()

    asyncio.run(scenario())


def test_old_ref_used_by_another_source_is_not_retired_until_unbound(tmp_path):
    async def scenario():
        async with _relay() as (origin, _, accepted):
            service, config, runtime, source, ref = await _source(tmp_path, origin, "bearer")
            alias = ModelHubSourceConfig.from_payload({**source.to_payload(), "id": "src_aliasfixture"})
            config.config.sources.append(alias)
            accepted.add("fixture-replacement")
            await service.replace_credential(source.id, {"key": "fixture-replacement"})
            assert len(service.revocations.list()) == 1
            assert runtime.state_store.read_api_key(ref) == KEY
            await service._ensure_engine_synced()
            assert len(service.revocations.list()) == 1
            assert runtime.state_store.read_api_key(ref) == KEY
            config.config.sources = [entry for entry in config.config.sources if entry.id != alias.id]
            await service._ensure_engine_synced()
            assert not service.revocations.list()
            assert not _document(runtime.state_store, ref).exists()

    asyncio.run(scenario())


def test_plain_oauth_identity_cannot_be_repaired_or_retired_as_api_key(tmp_path):
    async def scenario():
        runtime = _adapter(tmp_path)
        store = runtime.state_store
        store.audit_auth_permissions()
        auth = store.auth_dir / "fixture-oauth.json"
        store._secure_write_json(auth, {"fixture": "must survive"})
        ref = store.bind_oauth_credential("src_oauthfixture", "anthropic", auth.name)
        assert re.fullmatch(r"cred_[0-9a-f]{32}", ref)
        runtime.supervisor.client_if_running = lambda: pytest.fail("must not call OAuth management")
        with pytest.raises(EngineStateError):
            await runtime.credential_auth_scheme(ref)
        with pytest.raises(EngineStateError):
            await runtime.revoke_api_key_credential(ref)
        assert store.credential_metadata(ref)["kind"] == "oauth"
        assert auth.exists()

    asyncio.run(scenario())
