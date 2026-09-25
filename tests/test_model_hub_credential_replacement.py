"""Replacing a Source's credential: fresh verification, and no residue on cancel."""

import asyncio
import re

import pytest

from config.v2_config import (
    ModelHubModelConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub.oauth import OAuthFlowState
from core.handlers.model_hub.revocations import PendingCredentialRevocation
from core.handlers.model_hub.service import ModelHubError
from tests.test_model_hub_api import _create_source, _refresh_fixture_routes, _service

REPLACEMENTS = ["replace_credential", "retarget_base_url", "reauth"]


async def _replaceable_source(service, store, adapter, operation):
    """Seed a verified Source; return its old ref and a replacement starter."""

    if operation == "reauth":
        store.config.sources.append(
            ModelHubSourceConfig(
                id="src_huboauth01",
                kind="subscription",
                vendor="anthropic",
                display_name="Hub subscription",
                protocol="anthropic",
                supply_channel="hub",
                billing="monthly",
                state=ModelHubSourceStateConfig(status="standby"),
                models=[ModelHubModelConfig(id="claude-opus-4-6", provenance="discovered")],
                credential_ref="cred_hub_old",
            )
        )
        _refresh_fixture_routes(store.config)
        flow_id = (
            await service.reauth_source("src_huboauth01", {"acknowledge_irreversible": True})
        )["flow"]["flow_id"]
        adapter.flows[flow_id] = OAuthFlowState(
            **{
                **adapter.flows[flow_id].__dict__,
                "state": "success",
                "credential_ref": "cred_hub_new",
            }
        )
        return "cred_hub_old", lambda: service.oauth_status(flow_id)

    source = await _create_source(
        service,
        {
            "kind": "api_key",
            "vendor": "custom",
            "display_name": "Replaceable",
            "base_url": "https://old-relay.example/v1",
            "key": "sk-test-transient-only",
        },
    )
    if operation == "replace_credential":
        return source["credential_ref"], lambda: service.replace_credential(
            source["id"], {"key": "sk-test-replacement-key"},
        )
    return source["credential_ref"], lambda: service.patch_source(
        source["id"], {"base_url": "https://new-relay.example/v1"},
    )


@pytest.mark.parametrize("operation", REPLACEMENTS)
def test_replaced_credential_awaits_its_own_verification(tmp_path, operation):
    """Acceptance of the old credential is not evidence for its replacement."""

    service, store, adapter = _service(tmp_path)

    async def scenario():
        old_ref, replace = await _replaceable_source(service, store, adapter, operation)
        assert store.config.sources[0].verification_pending is None
        await replace()
        [source] = store.config.sources
        assert source.credential_ref != old_ref
        assert re.fullmatch(r"vp_[0-9a-f]{32}", source.verification_pending or "")

    asyncio.run(scenario())


@pytest.mark.parametrize("cleanup", ["revoked", "journaled", "unsettled"])
@pytest.mark.parametrize("operation", REPLACEMENTS)
def test_cancelled_replacement_keeps_old_credential_and_settles_the_new_one(
    tmp_path, operation, cleanup,
):
    """Cancel after the replacement exists but before it is persisted.

    The old credential stays authoritative, and the replacement is revoked, or
    journaled for replay when revoke fails. When neither is possible the
    operation reports `engine_down` rather than a plain cancellation, so an
    unrecorded credential is never silently abandoned.
    """

    service, store, adapter = _service(tmp_path)

    async def scenario():
        old_ref, replace = await _replaceable_source(service, store, adapter, operation)
        before = store.config.to_payload()
        revoked_before = list(adapter.revoked)
        discovering = asyncio.Event()
        replacement: list[str] = []

        async def discover_until_cancelled(vendor, protocol, base_url, credential_ref):
            replacement.append(credential_ref)
            discovering.set()
            await asyncio.Event().wait()

        adapter.discover_models = discover_until_cancelled
        revoke = adapter.revoke_credential

        async def revoke_credential(credential_ref):
            if cleanup != "revoked" and credential_ref in replacement:
                raise RuntimeError("engine unavailable")
            await revoke(credential_ref)

        adapter.revoke_credential = revoke_credential
        journal_add = service.revocations.add

        def add(source_id, credential_ref, **options):
            if cleanup == "unsettled" and credential_ref in replacement:
                raise OSError("journal unavailable")
            journal_add(source_id, credential_ref, **options)

        service.revocations.add = add

        task = asyncio.create_task(replace())
        await discovering.wait()
        task.cancel()
        if cleanup == "unsettled":
            with pytest.raises(ModelHubError) as exc_info:
                await task
            assert exc_info.value.code == "engine_down"
        else:
            with pytest.raises(asyncio.CancelledError):
                await task

        [replacement_ref] = replacement
        assert replacement_ref != old_ref
        assert store.config.to_payload() == before
        [source] = store.config.sources
        assert source.credential_ref == old_ref
        assert adapter.revoked == (
            [*revoked_before, replacement_ref] if cleanup == "revoked" else revoked_before
        )
        assert service.revocations.list() == (
            [PendingCredentialRevocation(source.id, replacement_ref)]
            if cleanup == "journaled"
            else []
        )

    asyncio.run(scenario())
