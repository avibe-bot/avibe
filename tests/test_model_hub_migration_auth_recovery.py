"""Persistent takeover direction also owns the in-memory authentication mirror."""

import asyncio
import subprocess
from copy import deepcopy

import psutil
import pytest

from config import paths
from config.v2_config import V2Config
from core.handlers.model_hub.adapter import OAuthCredentialRejectedError
from core.handlers.model_hub.migration import recover_native_migration
from core.handlers.model_hub.service import ModelHubError, V2ModelHubConfigStore
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import (
    _isolate_native_home,
    _service,
    _write_claude_oauth,
)
from tests.test_model_hub_migration_auth_mirror import _assert_mirrored, _forbidden, _runtime


@pytest.fixture(autouse=True)
def _native_boundaries(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess.Popen, "__init__", _forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _forbidden)
    monkeypatch.setattr(psutil, "process_iter", _forbidden)


def _persistent_takeover(monkeypatch, tmp_path, *, oauth=False):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(paths, "get_config_path", lambda: config_path)
    config = V2Config.default()
    config.model_hub.agents["claude"].mode = "direct"
    if oauth:
        _write_claude_oauth(home)
    else:
        config.agents.claude.auth_mode = "api_key"
        config.agents.claude.api_key = "fixture-old-key"
        config.agents.claude.base_url = "https://api.anthropic.com/fixture-path"
        config.agents.claude.auth_mode_set = True
    config.save(config_path=config_path)
    service, _, adapter = _service(tmp_path, migration_home=home)
    service.store = V2ModelHubConfigStore()
    runtime = _runtime(config)
    runtime.controller.model_hub_service = service
    service.migration_guard = runtime.coordinator.migration_guard
    before = service.store.native_auth_snapshot(("claude",))
    live = deepcopy(before)
    observations = []

    def mirror(snapshot):
        assert service._mutation_lock.locked()
        record = service.migration_journal.load()
        assert record is not None  # Never drop durable ownership first.
        assert snapshot == service.store.native_auth_snapshot(("claude",))
        assert runtime.admissions == runtime.turns == {"claude"}
        runtime.coordinator.reconcile_migration_auth(snapshot)
        _assert_mirrored(runtime, snapshot)
        observations.append((record["phase"], deepcopy(snapshot)))
        live.clear()
        live.update(deepcopy(snapshot))

    service.migration_reconcile_auth = mirror

    async def resume(backend, **kwargs):
        _assert_mirrored(runtime, service.store.native_auth_snapshot(("claude",)))
        assert backend not in runtime.admissions
        runtime.turns.discard(backend)

    runtime.controller.session_turns.end_backend_drain.side_effect = resume
    ids = [row["id"] for row in service.migration_scan()["items"]]
    return service, adapter, before, live, observations, ids, runtime


def test_live_auth_is_mirrored_before_publication_and_completed_receipt(monkeypatch, tmp_path):
    service, adapter, before, live, observations, ids, runtime = _persistent_takeover(monkeypatch, tmp_path)
    sync = adapter.sync_sources
    complete = service.migration_journal.complete

    def assert_after():
        assert live == service.store.native_auth_snapshot(("claude",))
        assert live["claude"]["api_key"] is None
        assert live["claude"]["base_url"] is None
        assert live != before

    async def checked_sync(bindings):
        assert_after()
        await sync(bindings)

    def checked_complete(record):
        assert_after()
        complete(record)

    adapter.sync_sources = checked_sync
    service.migration_journal.complete = checked_complete
    assert asyncio.run(service.migration_apply(ids))["applied"] == 1
    assert [phase for phase, _ in observations] == ["withdrawn", "exposed"]
    assert service.migration_journal.load() is None
    assert not runtime.admissions and not runtime.turns


@pytest.mark.parametrize("persistent", [False, True])
def test_preexposure_mirror_failure_recovers_the_restored_direction(monkeypatch, tmp_path, persistent):
    service, adapter, before, live, observations, ids, runtime = _persistent_takeover(monkeypatch, tmp_path)
    mirror = service.migration_reconcile_auth
    calls = []

    def fail(snapshot):
        calls.append(True)
        if persistent or len(calls) == 1:
            # A partial mirror must be repairable in the same persistent direction.
            live["claude"]["api_key"] = None
            runtime.controller.config.claude.api_key = None
            raise OSError("fixture mirror failure")
        mirror(snapshot)

    service.migration_reconcile_auth = fail
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids))
    assert service.store.native_auth_snapshot(("claude",)) == before
    assert not adapter.synced
    assert not adapter.activated
    if persistent:
        assert service.migration_journal.load()["phase"] == "reverting"
        assert service.migration_blocked_backends == {"claude"}
        assert runtime.admissions == runtime.turns == {"claude"}
        service.migration_reconcile_auth = mirror
        # Finishing a reversal rejects the old consent, but releases its owner.
        with pytest.raises(ValueError):
            asyncio.run(recover_native_migration(service))
    assert live == before
    assert observations[-1][0] == "reverting"
    assert service.migration_journal.load() is None
    assert not service.migration_blocked_backends
    _assert_mirrored(runtime, before)
    assert not runtime.admissions and not runtime.turns


def test_exposed_mirror_failure_keeps_current_ref_and_forward_only_recovery(monkeypatch, tmp_path):
    service, adapter, before, live, observations, ids, runtime = _persistent_takeover(monkeypatch, tmp_path)
    mirror = service.migration_reconcile_auth

    def fail_before_receipt(snapshot):
        if service.migration_journal.load()["phase"] == "exposed":
            raise OSError("fixture mirror completion failure")
        mirror(snapshot)

    service.migration_reconcile_auth = fail_before_receipt
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids))
    record = service.migration_journal.load()
    assert record["phase"] == "exposed"
    ref = service.store.load().sources[0].credential_ref
    assert not adapter.revoked
    assert service.store.native_auth_snapshot(("claude",)) != before
    assert service.migration_blocked_backends == {"claude"}
    assert runtime.admissions == runtime.turns == {"claude"}
    # Reopened consumers must converge even though persisted Hub config is equal.
    live.clear()
    live.update(deepcopy(before))
    runtime.owner.reconcile_native_auth_snapshot(before)
    service.migration_reconcile_auth = mirror
    asyncio.run(recover_native_migration(service))
    assert live == service.store.native_auth_snapshot(("claude",))
    assert live["claude"]["api_key"] is None
    assert observations[-1][0] == "exposed"
    assert len(adapter.provisioned) == 1
    assert service.store.load().sources[0].credential_ref == ref
    assert service.migration_journal.load() is None
    assert not service.migration_blocked_backends
    _assert_mirrored(runtime, service.store.native_auth_snapshot(("claude",)))
    assert not runtime.admissions and not runtime.turns


def test_terminal_oauth_recovery_mirrors_before_releasing_custody(monkeypatch, tmp_path):
    service, adapter, _, live, _, ids, runtime = _persistent_takeover(monkeypatch, tmp_path, oauth=True)
    mirror = service.migration_reconcile_auth

    async def rejected(ref):
        raise OAuthCredentialRejectedError()

    def fail_terminal(snapshot):
        if service.migration_journal.load().get("terminal"):
            raise OSError("fixture terminal mirror failure")
        mirror(snapshot)

    adapter.validate_oauth_credential = rejected
    service.migration_reconcile_auth = fail_terminal
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids))
    assert service.migration_journal.load()["terminal"]["invalid_source_ids"]
    assert service.migration_journal.completed() is None
    assert service.migration_blocked_backends == {"claude"}
    assert runtime.admissions == runtime.turns == {"claude"}
    ref = service.store.load().sources[0].credential_ref
    activated = list(adapter.activated)
    service.migration_reconcile_auth = mirror
    asyncio.run(recover_native_migration(service))
    assert live == service.store.native_auth_snapshot(("claude",))
    assert adapter.activated == activated  # Terminal retry cannot republish old OAuth.
    assert not adapter.revoked
    assert service.store.load().sources[0].credential_ref == ref
    assert service.store.load().sources[0].state.status == "needs_action"
    assert service.migration_journal.completed()["outcome"] == "needs_auth"
    assert service.migration_journal.load() is None
    assert not runtime.admissions and not runtime.turns
