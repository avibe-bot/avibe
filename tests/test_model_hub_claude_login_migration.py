"""CPA's login-save filename migration preserves Avibe's prefix ownership."""

import json

import pytest

from core.handlers.model_hub.adapter import RetainedMaterialDisposition
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.client import EngineClientError
from vibe.model_hub_runtime.state import EngineStateStore


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["same_source", "foreign_source", "legacy_retained", "patch_failure"])
async def test_claude_login_migration_preserves_existing_owner(tmp_path, case):
    # The pinned CPA login saves a canonical file with the old non-token
    # metadata, then deletes its predecessor. No restart/refresh rename is
    # simulated: this side effect happens only when the login completes.
    store = EngineStateStore(tmp_path / "engine")
    old_name = "claude-owner@example.com.json"
    new_name = "claude-00f765af-owner@example.com.json"
    source_id = "src_original123"
    ref = store.bind_oauth_credential(source_id, "anthropic", old_name)
    original = store.credential_metadata(ref)
    store.write_oauth_auth_file(old_name, {
        "type": "claude", "prefix": original["prefix"],
        "email": "owner@example.com", "refresh_token": "old-fixture",
    })
    patches = []
    deletes = []

    class Client:
        def management_request(self, method, path, *, query=None, payload=None, timeout=None):
            if (method, path) == ("GET", "/auth-files"):
                return {"files": [{
                    "id": file.name, "name": file.name, "provider": "claude",
                    "auth_index": str(index),
                } for index, file in enumerate(sorted(store.auth_dir.glob("*.json")))]}
            if (method, path) == ("GET", "/anthropic-auth-url"):
                return {"state": "fixture-login", "url": "https://example.test/oauth"}
            if (method, path) == ("GET", "/get-auth-status"):
                old_payload = json.loads((store.auth_dir / old_name).read_text())
                store.write_oauth_auth_file(new_name, {
                    **old_payload, "refresh_token": "new-fixture",
                })
                if case != "legacy_retained":
                    (store.auth_dir / old_name).unlink()
                return {"status": "ok"}
            if (method, path) == ("PATCH", "/auth-files/fields"):
                patches.append(payload)
                if case == "patch_failure":
                    raise EngineClientError("fixture patch failure")
                assert payload == {"name": new_name, "prefix": original["prefix"]}
                return {"status": "ok"}
            if (method, path) == ("DELETE", "/auth-files"):
                deletes.append(query["name"])
                store.delete_oauth_auth_file(query["name"])
                return {"status": "ok"}
            raise AssertionError((method, path))

    class Supervisor:
        def client(self):
            return client

        def with_engine_excluded(self, operation):
            return operation(client)

    client = Client()
    adapter = CLIProxyEngineAdapter(supervisor=Supervisor(), state_store=store)
    login_source = "src_foreign1234" if case == "foreign_source" else source_id
    flow = await adapter.start_oauth(login_source, "anthropic")
    result = await adapter.oauth_status(flow.flow_id)

    assert not deletes
    assert (store.auth_dir / new_name).is_file()
    assert json.loads((store.auth_dir / new_name).read_text())["prefix"] == original["prefix"]
    assert len(store._oauth_credentials()) == 1
    if case == "legacy_retained":
        assert result.state == "failed"
        assert result.error_key == "models.oauth.binding_failed"
        assert result.retained_material_disposition is RetainedMaterialDisposition.UNKNOWN
        assert result.retained_credential_ref is None
        assert store.credential_metadata(ref) == original
        assert (store.auth_dir / old_name).is_file()
        assert not patches
    else:
        assert store.credential_metadata(ref) == {**original, "auth_name": new_name}
        assert not (store.auth_dir / old_name).exists()
        if case == "foreign_source":
            assert result.state == "failed"
            assert result.error_key == "models.oauth.account_already_added"
            assert result.retained_material_disposition is RetainedMaterialDisposition.FOREIGN_SOURCE_REF
            assert result.retained_credential_ref is None
            assert not patches
        elif case == "patch_failure":
            assert result.state == "failed"
            assert result.error_key == "models.oauth.binding_failed"
            assert result.retained_material_disposition is RetainedMaterialDisposition.FLOW_SOURCE_REF
            assert result.retained_credential_ref == ref
        else:
            assert result.state == "success"
            assert result.credential_ref == ref
    assert await adapter.oauth_status(flow.flow_id) == result


@pytest.mark.asyncio
async def test_legacy_claude_grant_is_usable_without_relogin_or_migration(tmp_path):
    # A released v7.2 binding has no new identity metadata. CPA loads it under
    # its existing name; normal use must not require a login or a migration.
    store = EngineStateStore(tmp_path / "engine")
    auth_name = "claude-owner@example.com.json"
    ref = store.bind_oauth_credential("src_original123", "anthropic", auth_name)
    metadata = store.credential_metadata(ref)
    store.write_oauth_auth_file(auth_name, {
        "type": "claude", "prefix": metadata["prefix"],
        "email": "owner@example.com", "refresh_token": "legacy-fixture",
    })

    class Client:
        def management_request(self, method, path, *, query=None, **_kwargs):
            assert (method, path, query) == ("GET", "/auth-files/models", {"name": auth_name})
            return {"models": [{"id": "claude-sonnet-fixture"}]}

    class Supervisor:
        def client(self):
            return Client()

    adapter = CLIProxyEngineAdapter(supervisor=Supervisor(), state_store=store)
    before = {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    models = await adapter.discover_models("anthropic", "anthropic", None, ref)
    assert [model.id for model in models] == ["claude-sonnet-fixture"]
    assert adapter.subscription_account_label("src_original123", "anthropic", ref) == "owner@example.com"
    assert {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()} == before
