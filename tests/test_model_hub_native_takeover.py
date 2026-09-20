"""Ownership-boundary tests use only synthetic homes and engine doubles."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, nullcontext

import pytest

from core.handlers.model_hub.migration_journal import NativeFileEdit, NativeTakeoverJournal
from core.handlers.model_hub.service import EngineUnavailableError, ModelHubError
from config.v2_config import ModelHubConfig
from core.handlers.model_hub.adapter import (
    OAuthCredentialRejectedError,
    OAuthFlowState,
    RuntimePlatformUnsupportedError,
)
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import (
    _isolate_native_home,
    _service,
    _write,
    _write_claude_oauth,
    _write_codex_oauth,
)


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("native_present", [False, True])
@pytest.mark.parametrize("boundary", ["entry", "verify"])
def test_mode_only_adoption_checks_native_absence_inside_guard(
    monkeypatch, tmp_path, backend, native_present, boundary,
):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    service, store, adapter = _service(tmp_path, migration_home=home)
    store.config.agents[backend].mode = "direct"
    assert service.migration_scan()["items"] == []
    checks = []

    def login():
        if native_present:
            if backend == "claude":
                _write_claude_oauth(home)
            elif backend == "codex":
                _write_codex_oauth(home)
            else:
                _write(home / ".local/share/opencode/auth.json",
                       '{"openai":{"type":"api","key":"fixture-key"}}')

    @asynccontextmanager
    async def guard(backends):
        assert backends == (backend,)
        assert not service._mutation_lock.locked()
        if boundary == "entry":
            login()
        async def verify():
            checks.append(True)
            if boundary == "verify":
                login()
        yield verify

    service.migration_guard = guard
    if native_present:
        with pytest.raises(ModelHubError) as failure:
            asyncio.run(service.set_agent_mode(backend, "hub"))
        assert failure.value.code == "mode_switch_blocked"
        assert store.config.agents[backend].mode == "direct"
        assert service.migration_scan()["items"]
    else:
        assert asyncio.run(service.set_agent_mode(backend, "hub"))["mode"] == "hub"
        assert checks
    assert not store.config.sources
    assert not adapter.provisioned and not adapter.oauth_provisioned
    assert service.migration_journal.load() is None


def test_oauth_is_not_exposed_until_native_cleanup_and_mode_commit(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _write_claude_oauth(home)
    _isolate_native_home(monkeypatch, home)
    service, store, adapter = _service(tmp_path, migration_home=home)
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
    service, store, adapter = _service(tmp_path, migration_home=home)
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


@pytest.mark.parametrize(
    "error,code",
    [(EngineUnavailableError, "engine_down"),
     (RuntimePlatformUnsupportedError, "runtime_platform_unsupported")],
)
def test_unavailable_runtime_preserves_native_oauth_before_custody(monkeypatch, tmp_path, error, code):
    home = tmp_path / "native"
    _write_codex_oauth(home)
    _isolate_native_home(monkeypatch, home)
    native = home / ".codex/auth.json"
    before = native.read_bytes()
    service, store, adapter = _service(tmp_path, migration_home=home)
    store.config.agents["codex"].mode = "direct"
    previous = store.config.to_payload()
    item_ids = [item["id"] for item in service.migration_scan()["items"]]

    async def unavailable(**kwargs):
        raise error()

    adapter.ensure_installed = unavailable
    with pytest.raises(ModelHubError) as failure:
        asyncio.run(service.migration_apply(item_ids))
    assert failure.value.code == code
    assert native.read_bytes() == before
    assert store.config.to_payload() == previous
    assert not adapter.activated
    assert not adapter.synced
    assert service.migration_journal.load() is None
    assert not service.migration_blocked_backends


@pytest.mark.parametrize("legacy_receipt", [False, True])
@pytest.mark.parametrize("backend", ["opencode", "codex", "claude"])
def test_completed_receipt_recleans_resurrected_credentials_without_reimport(
    monkeypatch, tmp_path, backend, legacy_receipt,
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
    service, store, adapter = _service(tmp_path, migration_home=home)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert asyncio.run(service.migration_apply(ids))["applied"] == 1
    if legacy_receipt:
        # Published receipts predate snapshot-bound consent. Their item IDs
        # encode the same grant/target inventory, without the new display data.
        receipt = service.migration_journal.completed()
        for row, identity in zip(receipt["items"], receipt.pop("inventory_ids"), strict=True):
            row["id"] = identity
            row.pop("source_paths", None)
            row.pop("required_backends", None)
        NativeTakeoverJournal(service.migration_journal.path.with_name("last-completed.json")).save(receipt)
    current = store.config.to_payload()
    provisions = (len(adapter.provisioned), len(adapter.oauth_provisioned))

    # This is an external native writer, not an Avibe-owned auth operation.
    native.write_bytes(original)
    rescanned_ids = [row["id"] for row in service.migration_scan()["items"]]
    if rescanned_ids != ids:
        # Cleanup may have changed other configuration layers. Fresh consent
        # is required, but it must not provision the restored old OAuth again.
        with pytest.raises(ModelHubError):
            asyncio.run(service.migration_apply(ids))
    ids = rescanned_ids
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
    service, _, adapter = _service(tmp_path, migration_home=home)
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
    first, store, adapter = _service(tmp_path, migration_home=home)
    ids = [item["id"] for item in first.migration_scan()["items"]]
    validate = adapter.validate_oauth_credential

    async def offline(ref):
        raise RuntimeError("synthetic network outage")

    adapter.validate_oauth_credential = offline
    with pytest.raises(ModelHubError):
        asyncio.run(first.migration_apply(ids))
    second, _, _ = _service(tmp_path, migration_home=home)
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
    service, _, adapter = _service(tmp_path, migration_home=home)
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
    service, _, adapter = _service(tmp_path, migration_home=home)
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
    service, _, adapter = _service(tmp_path, migration_home=home)
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
    service, store, adapter = _service(tmp_path, migration_home=home)
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


@pytest.mark.parametrize("crash_before_receipt", [False, True])
def test_explicit_hub_reauth_can_repair_inconclusive_exposed_takeover(
    monkeypatch, tmp_path, crash_before_receipt,
):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    _write_codex_oauth(home)
    service, store, adapter = _service(tmp_path, migration_home=home)
    ids = [item["id"] for item in service.migration_scan()["items"]]

    async def inconclusive(ref):
        adapter.validated.append(ref)
        raise RuntimeError("fixture CPA status: token expired; no refresh provenance")

    adapter.validate_oauth_credential = inconclusive
    with pytest.raises(ModelHubError) as pending:
        asyncio.run(service.migration_apply(ids))
    assert pending.value.code == "migration_recovery_pending"
    source = store.config.sources[0]
    retained_ref = source.credential_ref
    starts = []

    async def start(source_id, vendor):
        assert service.migration_journal.load() is None
        assert not service.migration_blocked_backends
        assert store.config.sources[0].credential_ref == retained_ref
        starts.append(source_id)
        return OAuthFlowState(
            flow_id="oaf_fixture123", source_id=source_id, vendor=vendor,
            state="awaiting_action", auth_url="https://fixture.example/authorize",
            device_code=None, expects=None, instructions_key=None, error_key=None,
            expires_at_iso="2099-01-01T00:00:00+00:00", credential_ref=None,
        )

    adapter.start_oauth = start
    # The escape is an existing explicitly acknowledged reauthentication, not
    # an implicit interpretation of a 401/503 or a migration cancellation.
    with pytest.raises(ModelHubError) as unacknowledged:
        asyncio.run(service.reauth_source(source.id, {}))
    assert unacknowledged.value.code == "reauth_confirmation_required"
    assert service.migration_journal.load()["phase"] == "exposed"

    complete = service.migration_journal.complete
    if crash_before_receipt:
        def disk_unavailable(record):
            raise OSError("fixture crash before receipt")

        service.migration_journal.complete = disk_unavailable
        with pytest.raises(ModelHubError):
            asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))
        assert service.migration_journal.load()["terminal"]["reason"] == "reauth_requested"
        assert starts == []
        service.migration_journal.complete = complete
        asyncio.run(service.recover_runtime_intent())
    result = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))
    assert result["flow"]["channel"] == "hub"
    assert result["flow"]["intent"] == "reauth"
    assert starts == [source.id]
    assert service.migration_journal.completed()["outcome"] == "reauth_requested"
    assert adapter.revoked == []
    assert len(adapter.validated) == 1
    assert not (home / ".codex/auth.json").exists()
    assert store.config.sources[0].state.status == "needs_action"
    with pytest.raises(ModelHubError) as replay:
        asyncio.run(service.migration_apply(ids))
    assert replay.value.code != "migration_credentials_invalid"


def test_explicit_reauth_keeps_verified_sibling_usable(monkeypatch, tmp_path):
    from core.handlers.model_hub.migration import prepare_takeover_reauthentication

    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    _write_claude_oauth(home)
    _write_codex_oauth(home)
    service, store, adapter = _service(tmp_path, migration_home=home)
    ids = [row["id"] for row in service.migration_scan()["items"]]

    async def validate(ref):
        source = next(source for source in store.config.sources if source.credential_ref == ref)
        adapter.validated.append(ref)
        if source.vendor == "openai":
            raise RuntimeError("fixture inconclusive validation")

    adapter.validate_oauth_credential = validate
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids))
    claude = next(source for source in store.config.sources if source.vendor == "anthropic")
    codex = next(source for source in store.config.sources if source.vendor == "openai")
    assert service.migration_journal.load()["validated_source_ids"] == [claude.id]
    asyncio.run(prepare_takeover_reauthentication(service, codex.id))
    assert store.config.sources[0].state.status == claude.state.status == "standby"
    assert next(source for source in store.config.sources if source.id == codex.id).state.status == "needs_action"
    assert len(adapter.validated) == 2
    assert adapter.revoked == []


@pytest.mark.parametrize("boundary", ["terminal_config", "receipt"])
def test_rejected_grant_terminal_decision_recovers_after_crash(monkeypatch, tmp_path, boundary):
    home = tmp_path / "native"
    _write_claude_oauth(home)
    _isolate_native_home(monkeypatch, home)
    service, store, adapter = _service(tmp_path, migration_home=home)
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
