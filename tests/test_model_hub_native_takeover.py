"""Ownership-boundary tests use only synthetic homes and engine doubles."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext

import pytest

from core.handlers.model_hub.migration_journal import NativeFileEdit, NativeTakeoverJournal
from core.handlers.model_hub.service import ModelHubError
from config.v2_config import ModelHubConfig
from core.handlers.model_hub.adapter import OAuthCredentialRejectedError
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import (
    _isolate_native_home,
    _service,
    _write,
    _write_claude_oauth,
    _write_codex_oauth,
)


def test_oauth_is_not_exposed_until_native_cleanup_and_mode_commit(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _write_claude_oauth(home)
    _isolate_native_home(monkeypatch, home)
    service, store, adapter = _service(tmp_path)
    store.config.agents["claude"].mode = "direct"
    item_ids = [item["id"] for item in service.migration_scan()["items"]]
    activate = adapter.activate_oauth_credential
    synced = adapter.sync_sources

    async def checked_sync(bindings):
        assert service.migration_journal.load()["phase"] == "exposed"
        assert adapter.activated
        await synced(bindings)

    adapter.sync_sources = checked_sync

    async def checked_activate(ref):
        assert not (home / ".claude/.credentials.json").exists()
        assert store.config.agents["claude"].mode == "hub"
        assert service.migration_journal.load()["phase"] == "exposed"
        await activate(ref)

    adapter.activate_oauth_credential = checked_activate
    result = asyncio.run(service.migration_apply(item_ids))
    assert result["applied"] == 1
    assert adapter.validated == adapter.activated
    assert service.migration_journal.load() is None
    assert not service.migration_blocked_backends
    assert asyncio.run(service.migration_apply(item_ids))["applied"] == 1
    assert len(adapter.oauth_provisioned) == 1
    assert "refresh_token" not in service.migration_journal.path.with_name("last-completed.json").read_text()


def test_failure_after_possible_rotation_retains_current_owner_and_retries(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _write_codex_oauth(home)
    _isolate_native_home(monkeypatch, home)
    service, store, adapter = _service(tmp_path)
    store.config.agents["codex"].mode = "direct"
    item_ids = [item["id"] for item in service.migration_scan()["items"]]
    activate = adapter.activate_oauth_credential
    rotated = {}

    async def activate_then_fail(ref):
        await activate(ref)
        rotated[ref] = "synthetic-current-refresh-token"
        raise RuntimeError("synthetic transport failure after rotation")

    adapter.activate_oauth_credential = activate_then_fail
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(item_ids))
    assert service.migration_journal.load()["phase"] == "exposed"
    assert store.config.agents["codex"].mode == "hub"
    assert not (home / ".codex/auth.json").exists()
    assert adapter.revoked == []
    assert service.migration_blocked_backends == {"codex"}
    assert [item["id"] for item in service.migration_scan()["items"]] == item_ids

    async def resume(ref):
        assert rotated[ref] == "synthetic-current-refresh-token"
        await activate(ref)

    adapter.activate_oauth_credential = resume
    assert asyncio.run(service.migration_apply(item_ids))["applied"] == 1
    assert len(adapter.oauth_provisioned) == 1
    assert service.migration_journal.load() is None


@pytest.mark.parametrize("backend", ["opencode", "codex", "claude"])
def test_completed_receipt_recleans_resurrected_credentials_without_reimport(
    monkeypatch, tmp_path, backend,
):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    if backend == "opencode":
        native = home / ".config/opencode/opencode.json"
        _write(native, json.dumps({
            "provider": {"openai": {"options": {"apiKey": "fixture-old-key"}}},
            "mcp": {"keep": True},
        }))
    elif backend == "codex":
        _write_codex_oauth(home)
        native = home / ".codex/auth.json"
    else:
        _write_claude_oauth(home)
        native = home / ".claude/.credentials.json"
    original = native.read_bytes()
    service, store, adapter = _service(tmp_path)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert asyncio.run(service.migration_apply(ids))["applied"] == 1
    current = store.config.to_payload()
    provisions = (len(adapter.provisioned), len(adapter.oauth_provisioned))

    # This is an external native writer, not an Avibe-owned auth operation.
    native.write_bytes(original)
    assert [row["id"] for row in service.migration_scan()["items"]] == ids
    assert asyncio.run(service.migration_apply(ids))["applied"] == 1
    assert service.migration_scan()["items"] == []
    assert store.config.to_payload() == current
    assert (len(adapter.provisioned), len(adapter.oauth_provisioned)) == provisions
    assert service.migration_journal.load() is None
    assert adapter.revoked == []


def test_completed_receipt_does_not_authorize_new_unconsented_native_key(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    native = home / ".config/opencode/opencode.json"
    _write(native, '{"provider":{"openai":{"options":{"apiKey":"fixture-first"}}}}')
    service, _, adapter = _service(tmp_path)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    asyncio.run(service.migration_apply(ids))
    changed = '{"provider":{"openai":{"options":{"apiKey":"fixture-new"}}}}'
    native.write_text(changed)
    with pytest.raises(ModelHubError) as failure:
        asyncio.run(service.migration_apply(ids))
    assert failure.value.code == "migration_item_conflict"
    assert native.read_text() == changed
    assert len(adapter.provisioned) == 1


def test_restart_recovers_exposed_handoff_without_native_tokens(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _write_claude_oauth(home)
    _isolate_native_home(monkeypatch, home)
    first, store, adapter = _service(tmp_path)
    ids = [item["id"] for item in first.migration_scan()["items"]]
    validate = adapter.validate_oauth_credential

    async def offline(ref):
        raise RuntimeError("synthetic network outage")

    adapter.validate_oauth_credential = offline
    with pytest.raises(ModelHubError):
        asyncio.run(first.migration_apply(ids))
    second, _, _ = _service(tmp_path)
    second.store = store
    second.adapter = adapter
    adapter.validate_oauth_credential = validate
    asyncio.run(second.recover_runtime_intent())
    assert not second.migration_blocked_backends
    assert second.migration_journal.load() is None
    assert len(adapter.oauth_provisioned) == 1


def test_caller_cancellation_does_not_abandon_credential_custody(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _write_claude_oauth(home)
    _isolate_native_home(monkeypatch, home)
    service, _, adapter = _service(tmp_path)
    ids = [item["id"] for item in service.migration_scan()["items"]]

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()
        activate = adapter.activate_oauth_credential

        async def paused(ref):
            entered.set()
            await release.wait()
            await activate(ref)

        adapter.activate_oauth_credential = paused
        task = asyncio.create_task(service.migration_apply(ids))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        assert (await task)["applied"] == 1

    asyncio.run(exercise())
    assert service.migration_journal.load() is None
    assert adapter.revoked == []


def test_native_compare_and_swap_does_not_erase_a_concurrent_login(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _write_claude_oauth(home)
    _isolate_native_home(monkeypatch, home)
    service, _, adapter = _service(tmp_path)
    ids = [item["id"] for item in service.migration_scan()["items"]]
    path = home / ".claude/.credentials.json"
    concurrent = '{"claudeAiOauth":{"accessToken":"different-user","refreshToken":"different-grant"}}'

    async def changed_during_preparation():
        path.write_text(concurrent)

    service.migration_guard = lambda backends: nullcontext(changed_during_preparation)
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids))
    assert path.read_text() == concurrent
    assert adapter.activated == []
    assert service.migration_journal.load()["phase"] == "reverting"


def test_journal_roundtrip_and_file_edits_are_private_and_replayable(tmp_path):
    path = tmp_path / "credentials.json"
    _write(path, '{"login":"synthetic","mcp":{"name":"保留"}}')
    before = path.read_bytes()
    after = b'{"mcp":{"name":"\\u4fdd\\u7559"}}'
    edit = NativeFileEdit.plan(path, after)
    journal = NativeTakeoverJournal(tmp_path / "takeover/current.json")
    journal.save({
        "version": 1, "phase": "prepared", "items": [],
        "backends": ["claude"], "files": [edit.to_payload()],
        "source_ids": [], "credentials": [],
        "previous": ModelHubConfig().to_payload(),
        "updated": ModelHubConfig().to_payload(),
    })
    assert journal.path.stat().st_mode & 0o777 == 0o600
    loaded = NativeFileEdit.from_payload(journal.load()["files"][0])
    loaded.apply()
    loaded.apply()
    assert path.read_bytes() == after
    loaded.apply(reverse=True)
    assert path.read_bytes() == before


def test_invalid_journal_does_not_start_engine_or_admit_native_backends(monkeypatch, tmp_path):
    home = tmp_path / "native"
    home.mkdir()
    _isolate_native_home(monkeypatch, home)
    service, _, adapter = _service(tmp_path)
    path = service.migration_journal.path
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("not-json")
    path.chmod(0o600)
    with pytest.raises(ModelHubError):
        asyncio.run(service.recover_runtime_intent())
    assert service.migration_blocked_backends == {"claude", "codex", "opencode"}
    assert adapter.activated == []


def test_rejected_refresh_finishes_custody_without_claiming_success(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _write_claude_oauth(home)
    _write_codex_oauth(home)
    _isolate_native_home(monkeypatch, home)
    service, store, adapter = _service(tmp_path)
    ids = [item["id"] for item in service.migration_scan()["items"]]
    validate = adapter.validate_oauth_credential

    async def reject_claude(ref):
        await validate(ref)
        claude = next(source for source in store.config.sources if source.vendor == "anthropic")
        if ref == claude.credential_ref:
            raise OAuthCredentialRejectedError()

    adapter.validate_oauth_credential = reject_claude
    with pytest.raises(ModelHubError) as failure:
        asyncio.run(service.migration_apply(ids))
    assert failure.value.code == "migration_credentials_invalid"
    assert len(adapter.validated) == 2
    assert service.migration_journal.load() is None
    assert not service.migration_blocked_backends
    assert not (home / ".claude/.credentials.json").exists()
    assert not (home / ".codex/auth.json").exists()
    assert adapter.revoked == []
    claude = next(source for source in store.config.sources if source.vendor == "anthropic")
    codex = next(source for source in store.config.sources if source.vendor == "openai")
    assert claude.state.status == "needs_action"
    assert codex.state.status != "needs_action"
    assert service.migration_journal.completed()["outcome"] == "needs_auth"
    with pytest.raises(ModelHubError) as repeated:
        asyncio.run(service.migration_apply(ids))
    assert repeated.value.code == "migration_credentials_invalid"
    assert len(adapter.validated) == 2
    # Normal Hub config/auth repair is not trapped behind the migration gate.
    service._save_config(service._clone_config(store.config))


@pytest.mark.parametrize("boundary", ["terminal_config", "receipt"])
def test_rejected_grant_terminal_decision_recovers_after_crash(monkeypatch, tmp_path, boundary):
    home = tmp_path / "native"
    _write_claude_oauth(home)
    _isolate_native_home(monkeypatch, home)
    service, store, adapter = _service(tmp_path)
    ids = [item["id"] for item in service.migration_scan()["items"]]

    async def rejected(ref):
        adapter.validated.append(ref)
        raise OAuthCredentialRejectedError()

    adapter.validate_oauth_credential = rejected
    save = store.save
    complete = service.migration_journal.complete

    def fail_terminal(config):
        if any(source.state.status == "needs_action" for source in config.sources):
            raise OSError("synthetic persistence failure")
        save(config)

    def fail_receipt(record):
        raise OSError("synthetic receipt failure")

    if boundary == "terminal_config":
        store.save = fail_terminal
    else:
        service.migration_journal.complete = fail_receipt
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids))
    record = service.migration_journal.load()
    assert record["phase"] == "exposed"
    assert record["terminal"]["invalid_source_ids"]
    assert service.migration_blocked_backends == {"claude"}
    store.save = save
    service.migration_journal.complete = complete
    asyncio.run(service.recover_runtime_intent())
    assert len(adapter.validated) == 1
    assert service.migration_journal.load() is None
    assert not service.migration_blocked_backends
    assert store.config.sources[0].state.status == "needs_action"
    assert adapter.revoked == []
