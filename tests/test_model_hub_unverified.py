"""Unverified configuration is persistable, never authentication evidence."""

import asyncio
import copy
import multiprocessing
import os
from pathlib import Path
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from config.v2_config import ModelHubSourceConfig, V2Config, config_write_transaction
from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind, SOURCE_PROTOCOLS
from core.handlers.model_hub.service import ModelHubError, V2ModelHubConfigStore
from tests.test_model_hub_api import _assert_valid, _service
from vibe.model_hub_runtime.api_key_vendors import api_key_vendor_catalog


def _draft(**changes):
    return {
        "kind": "api_key",
        "vendor": "custom",
        "protocol": "openai_chat",
        "base_url": "https://relay.example/v1",
        "key": "unverified-test-key",
        "client_nonce": "scn_unverified00000001",
        "save_unverified": True,
        **changes,
    }


@pytest.mark.parametrize(
    "owner",
    [{"vendor": entry.id} for entry in api_key_vendor_catalog()]
    + [{"vendor": "custom", "protocol": protocol} for protocol in SOURCE_PROTOCOLS],
)
def test_unverified_save_uses_declared_owner_without_upstream_work(tmp_path, owner):
    service, store, adapter = _service(tmp_path)
    adapter.observe_source = AsyncMock(side_effect=AssertionError("unexpected observation"))
    adapter.discover_models = AsyncMock(side_effect=AssertionError("unexpected inventory"))
    draft = _draft(**owner)
    if owner["vendor"] != "custom":
        draft.pop("protocol")
    created = asyncio.run(service.create_source(draft))["source"]
    _assert_valid("source.schema.json", created)
    assert created["verification_pending"]
    assert created["models"] == []
    assert created["state"]["status"] == "standby"
    assert created["client_nonce"] == draft["client_nonce"]
    assert adapter.credential_count == 1
    assert len(store.config.sources) == 1
    adapter.observe_source.assert_not_awaited()
    adapter.discover_models.assert_not_awaited()
    with pytest.raises(ModelHubError):
        asyncio.run(service.create_source(draft))
    assert adapter.credential_count == 1


@pytest.mark.parametrize("invalid", [
    {"protocol": None}, {"protocol": "unknown"}, {"key": " "},
    {"base_url": "file:///tmp/credentials"}, {"save_unverified": "true"},
    {"verification_pending": False}, {"vendor": "openai", "protocol": "anthropic"},
])
def test_unverified_consent_cannot_bypass_input_or_owner_validation(tmp_path, invalid):
    service, store, adapter = _service(tmp_path)
    draft = _draft(**invalid)
    if draft.get("protocol") is None:
        draft.pop("protocol")
    with pytest.raises(ModelHubError):
        asyncio.run(service.create_source(draft))
    assert store.config.sources == []
    assert adapter.credential_count == 0


def test_unverified_create_rolls_back_custody_and_releases_nonce(tmp_path):
    service, store, adapter = _service(tmp_path)
    adapter.fail_sync = True
    with pytest.raises(ModelHubError):
        asyncio.run(service.create_source(_draft()))
    assert not store.config.sources
    assert adapter.revoked == ["cred_test001"]
    adapter.fail_sync = False
    created = asyncio.run(service.create_source(_draft()))["source"]
    assert created["verification_pending"]
    assert len(store.config.sources) == 1


def test_inventory_never_clears_pending_verification(tmp_path):
    service, store, _adapter = _service(tmp_path)
    source = asyncio.run(service.create_source(_draft()))["source"]
    refreshed = asyncio.run(service.refresh_source(source["id"]))["source"]
    assert refreshed["models"]
    assert refreshed["verification_pending"] == source["verification_pending"]
    assert ModelHubSourceConfig.from_payload(store.config.sources[0].to_payload()).verification_pending


def test_verification_bookkeeping_io_failure_preserves_success_and_pending_state(tmp_path, monkeypatch):
    service, store, _adapter = _service(tmp_path)

    async def scenario():
        source = (await service.create_source(_draft()))["source"]
        outcome = RawCallOutcome(
            kind=RawOutcomeKind.SUCCESS, http_status=200, error_code=None,
            redacted_message=None, stream_started=False, model_id="test-model", source_id=source["id"],
        )
        def fail_save(_config):
            raise OSError("Disk unavailable")
        monkeypatch.setattr(store, "save", fail_save)
        await service._verify_successful_source(source["id"], source["credential_ref"], source["verification_pending"], outcome)
        assert store.config.sources[0].verification_pending == source["verification_pending"]

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", list(RawOutcomeKind))
@pytest.mark.parametrize("current", ["same", "replaced", "same_handle_reauth", "newer_attempt", "deleted"])
def test_only_current_successful_invocation_retires_verification(tmp_path, kind, current):
    service, store, _adapter = _service(tmp_path)

    async def scenario():
        source = (await service.create_source(_draft()))["source"]
        outcome = RawCallOutcome(
            kind=kind, http_status=200 if kind is RawOutcomeKind.SUCCESS else 400,
            error_code=None, redacted_message=None, stream_started=False,
            source_id=source["id"], model_id="test-model",
        )
        service._reserve_settlement_generation(source["id"])
        if current == "replaced":
            store.config.sources[0].credential_ref = "cred_replacement"
        elif current == "same_handle_reauth":
            service._mark_source_unverified(store.config.sources[0])
        elif current == "newer_attempt":
            service._reserve_settlement_generation(source["id"])
        elif current == "deleted":
            store.config.sources.clear()
        await service._verify_successful_source(source["id"], source["credential_ref"], source["verification_pending"], outcome)
        if current != "deleted":
            verified = current in {"same", "newer_attempt"} and kind is RawOutcomeKind.SUCCESS
            assert (store.config.sources[0].verification_pending is None) == verified

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["resolve", "probe", "stream"])
def test_model_call_consumers_retire_pending_verification(tmp_path, path):
    service, store, adapter = _service(tmp_path)

    async def scenario():
        source = (await service.create_source(_draft(vendor="anthropic", protocol="anthropic")))["source"]
        await service.refresh_source(source["id"])
        if path == "probe":
            result = await service.probe_agent("claude", "claude-opus-4-6")
            assert result["reachable"] is True
        else:
            resolved = await service.resolve(backend="claude", model_id="claude-opus-4-6", request={})
            if path == "stream":
                # Exercise the downstream-consumed settlement independently of
                # the resolver's buffered-success path, with the same identity.
                store.config.sources[0].verification_pending = resolved.verification_pending
                handle = await adapter.invoke(source["id"], "claude-opus-4-6", {}, False, "claude")
                outcome = replace(await handle.outcome(), stream_started=True)
                await service.settle_handle_outcome(
                    resolved, outcome, termination_origin="upstream_terminal", record_attempt=lambda *_args: None,
                )
        assert store.config.sources[0].verification_pending is None
        assert "verification_pending" not in service.list_sources()[0]

    asyncio.run(scenario())


@pytest.mark.parametrize("unverified", [False, True])
def test_new_api_key_credentials_always_start_pending(tmp_path, unverified):
    service, _store, _adapter = _service(tmp_path)
    created = asyncio.run(service.create_source(_draft(save_unverified=unverified)))["source"]
    _assert_valid("source.schema.json", created)
    assert created["verification_pending"]


def _settle_in_controller(config_home, captured, attempted, done):
    os.environ["AVIBE_HOME"] = config_home

    class SignallingStore(V2ModelHubConfigStore):
        def mutate(self, mutator):
            attempted.set()
            super().mutate(mutator)

    service, _store, _adapter = _service(Path(config_home) / "controller")
    service.store = SignallingStore()
    outcome = RawCallOutcome(
        kind=RawOutcomeKind.SUCCESS, http_status=200, error_code=None,
        redacted_message=None, stream_started=False, model_id="test-model", source_id=captured["id"],
    )
    asyncio.run(service._verify_successful_source(
        captured["id"], captured["credential_ref"], captured["verification_pending"], outcome,
    ))
    done.set()


@pytest.mark.parametrize("replacement", ["none", "same_handle", "new_handle"])
def test_controller_verification_preserves_fresh_web_config_and_credential_identity(tmp_path, monkeypatch, replacement):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    service, store, _adapter = _service(tmp_path)
    captured = asyncio.run(service.create_source(_draft()))["source"]
    config = V2Config.default()
    config.model_hub = store.load()
    config.save()
    ctx = multiprocessing.get_context("spawn")
    attempted, done = ctx.Event(), ctx.Event()
    controller = ctx.Process(target=_settle_in_controller, args=(str(tmp_path), captured, attempted, done))
    try:
        with config_write_transaction() as fresh:
            controller.start()
            assert attempted.wait(timeout=15), "controller never reached the verification transaction"
            assert not done.wait(timeout=1), "controller bypassed Web's cross-process lock"
            current = fresh.model_hub.sources[0]
            current.display_name = "Edited in Web"
            if replacement != "none":
                service._mark_source_unverified(current)
                if replacement == "new_handle":
                    current.credential_ref = "cred_web_replacement"
            pending = current.verification_pending
            peer = copy.deepcopy(current)
            peer.id = "src_webadded01"
            peer.client_nonce = None
            peer.credential_ref = "cred_web_added"
            fresh.model_hub.sources.append(peer)
            fresh.model_hub.agents["codex"].sources.order = [peer.id, current.id]
            fresh.language = "zh"
        controller.join(timeout=15)
        assert controller.exitcode == 0
        assert done.is_set()
    finally:
        if controller.is_alive():
            controller.terminate()
            controller.join(timeout=5)
    updated = V2Config.load()
    current = updated.model_hub.sources[0]
    assert current.verification_pending == (None if replacement == "none" else pending)
    assert current.display_name == "Edited in Web"
    assert updated.model_hub.sources[1].to_payload() == peer.to_payload()
    assert updated.model_hub.agents["codex"].sources.order == [peer.id, current.id]
    assert updated.language == "zh"
