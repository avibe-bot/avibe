"""Source identity stays source-bound and useful across repeated subscriptions."""

import asyncio
import json
from dataclasses import replace

import pytest

from config.v2_config import ModelHubConfig
from core.handlers.model_hub.service import seeded_source_name
from tests.test_model_hub_api import _service
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter, _OAUTH_ENDPOINTS
from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore


async def _completed_flow(service, adapter, vendor):
    flow = (await service.oauth_start({"vendor": vendor, "channel": "hub"}))["flow"]
    adapter.flows[flow["flow_id"]] = replace(
        adapter.flows[flow["flow_id"]],
        state="success",
        credential_ref=f"cred_{flow['source_id']}",
    )
    return flow


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_subscription_names_are_allocated_at_commit_and_oauth_replays_keep_them(tmp_path, vendor):
    async def scenario():
        service, store, adapter = _service(tmp_path)
        first_flow = await _completed_flow(service, adapter, vendor)
        first = (await service.oauth_status(first_flow["flow_id"]))["source"]
        flows = [await _completed_flow(service, adapter, vendor) for _ in range(2)]
        created = await asyncio.gather(*(service.oauth_status(flow["flow_id"]) for flow in flows))
        seed = seeded_source_name(vendor)
        assert first["display_name"] == seed
        assert {result["source"]["display_name"] for result in created} == {f"{seed} 2", f"{seed} 3"}
        for flow, result in zip(flows, created):
            assert (await service.oauth_status(flow["flow_id"]))["source"] == result["source"]
        assert len(store.config.sources) == 3
        reloaded = ModelHubConfig.from_payload(store.config.to_payload())
        assert [row.display_name for row in reloaded.sources] == [seed, f"{seed} 2", f"{seed} 3"]
        assert adapter.revoked == []

    asyncio.run(scenario())


def test_subscription_allocation_respects_reserved_names_and_explicit_custom_names(tmp_path):
    async def scenario():
        service, store, adapter = _service(tmp_path)
        for name in ("OpenAI", "OpenAI 2", "Work subscription"):
            flow = await _completed_flow(service, adapter, "openai")
            result = await service.create_source({
                "kind": "subscription", "vendor": "openai", "supply_channel": "hub",
                "display_name": name, "oauth_flow_ref": flow["flow_id"],
            })
            assert result["source"]["display_name"] == name
        flow = await _completed_flow(service, adapter, "openai")
        result = await service.oauth_status(flow["flow_id"])
        assert result["source"]["display_name"] == "OpenAI 3"
        assert [source.display_name for source in store.config.sources[:3]] == [
            "OpenAI", "OpenAI 2", "Work subscription",
        ]

    asyncio.run(scenario())


def test_explicit_default_looking_names_are_preserved_and_omitted_names_are_allocated(tmp_path):
    async def scenario():
        service, store, adapter = _service(tmp_path)
        for _ in range(2):
            flow = await _completed_flow(service, adapter, "openai")
            result = await service.create_source({
                "kind": "subscription", "vendor": "openai", "supply_channel": "hub",
                "display_name": "OpenAI", "oauth_flow_ref": flow["flow_id"],
            })
            assert result["source"]["display_name"] == "OpenAI"
            assert (await service.oauth_status(flow["flow_id"]))["source"] == result["source"]
        flow = await _completed_flow(service, adapter, "openai")
        result = await service.create_source({
            "kind": "subscription", "vendor": "openai", "supply_channel": "hub",
            "oauth_flow_ref": flow["flow_id"],
        })
        assert result["source"]["display_name"] == "OpenAI 2"
        assert [source.display_name for source in store.config.sources] == ["OpenAI", "OpenAI", "OpenAI 2"]

    asyncio.run(scenario())


def _bound_account(tmp_path, vendor="openai", **identity):
    store = EngineStateStore(tmp_path / "engine")
    source_id = "src_identity001"
    ref = store.bind_oauth_credential(source_id, vendor, "account.json")
    prefix = store.credential_metadata(ref)["prefix"]
    payload = {
        "type": _OAUTH_ENDPOINTS[vendor][2], "prefix": prefix,
        "access_token": "private-access-fixture", "refresh_token": "private-refresh-fixture",
        **identity,
    }
    store.write_oauth_auth_file("account.json", payload)
    adapter = CLIProxyEngineAdapter(supervisor=object(), state_store=store)
    return store, adapter, source_id, ref, payload


@pytest.mark.parametrize("vendor", ["openai", "anthropic", "gemini", "kimi", "xai"])
def test_account_metadata_is_read_without_starting_engine_or_mutating_credentials(tmp_path, vendor):
    store, adapter, source_id, ref, _ = _bound_account(
        tmp_path, vendor, email="用户@example.com", username="fixture-user",
    )
    before = {path: (path.read_bytes(), path.stat().st_mode) for path in store.root.rglob("*") if path.is_file()}
    assert adapter.subscription_account_label(source_id, vendor, ref) == "用户@example.com"
    assert {path: (path.read_bytes(), path.stat().st_mode) for path in before} == before
    assert adapter.subscription_account_label("src_other001", vendor, ref) is None
    other_vendor = "openai" if vendor != "openai" else "anthropic"
    assert adapter.subscription_account_label(source_id, other_vendor, ref) is None


def test_claude_filename_migration_preserves_ref_source_and_prefix(tmp_path):
    store = EngineStateStore(tmp_path / "engine")
    old_name = "claude-user@example.com.json"
    new_name = "claude-00f765af-user@example.com.json"
    ref = store.bind_oauth_credential("src_identity001", "anthropic", old_name)
    prefix = store.credential_metadata(ref)["prefix"]
    identity = {
        "type": "claude",
        "prefix": prefix,
        "email": "user@example.com",
        "account_uuid": "account-a",
        "organization_uuid": "organization-a",
        "access_token": "private-access-fixture",
        "refresh_token": "private-refresh-fixture",
    }
    store.write_oauth_auth_file(old_name, identity)

    assert store.reconcile_oauth_auth_file(old_name, auth_provider="claude") == ref
    stored = store.credential_metadata(ref)
    assert stored["oauth_identity"]["organization_uuid"] == "organization-a"

    (store.auth_dir / old_name).rename(store.auth_dir / new_name)
    assert store.reconcile_oauth_auth_file(new_name, auth_provider="claude") == ref
    migrated = store.credential_metadata(ref)
    assert migrated["auth_name"] == new_name
    assert migrated["source_id"] == "src_identity001"
    assert migrated["prefix"] == prefix


def test_claude_filename_migration_backfills_released_legacy_metadata_from_prefix(tmp_path):
    store = EngineStateStore(tmp_path / "engine")
    old_name = "claude-user@example.com.json"
    new_name = "claude-00f765af-user@example.com.json"
    ref = store.bind_oauth_credential("src_identity001", "anthropic", old_name)
    prefix = store.credential_metadata(ref)["prefix"]
    identity = {
        "type": "claude",
        "prefix": prefix,
        "email": "user@example.com",
        "account_uuid": "account-a",
        "organization_uuid": "organization-a",
        "access_token": "private-access-fixture",
    }
    store.write_oauth_auth_file(old_name, identity)
    (store.auth_dir / old_name).rename(store.auth_dir / new_name)

    # This is the released v7.2 shape: auth_name and prefix only. Do not first
    # reconcile the old name, because that would seed the new field and hide
    # the upgrade path under test.
    metadata_path = store._credential_path(ref)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("oauth_identity", None)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    metadata_path.chmod(0o600)

    assert store.reconcile_oauth_auth_file(new_name, auth_provider="claude") == ref
    migrated = store.credential_metadata(ref)
    assert migrated["auth_name"] == new_name
    assert migrated["prefix"] == prefix
    assert migrated["oauth_identity"]["organization_uuid"] == "organization-a"


def test_claude_organization_only_identity_is_not_persisted(tmp_path):
    store = EngineStateStore(tmp_path / "engine")
    auth_name = "claude-user@example.com.json"
    ref = store.bind_oauth_credential("src_identity001", "anthropic", auth_name)
    prefix = store.credential_metadata(ref)["prefix"]
    store.write_oauth_auth_file(
        auth_name,
        {
            "type": "claude",
            "prefix": prefix,
            "organization_uuid": "organization-a",
            "access_token": "private-access-fixture",
        },
    )

    assert store.reconcile_oauth_auth_file(auth_name, auth_provider="claude") == ref
    assert "oauth_identity" not in store.credential_metadata(ref)
    assert store.reconcile_oauth_auth_file(auth_name, auth_provider="claude") == ref
    assert "oauth_identity" not in store.credential_metadata(ref)


def test_claude_filename_migration_rejects_cross_source_rebind(tmp_path):
    store = EngineStateStore(tmp_path / "engine")
    old_name = "claude-user@example.com.json"
    new_name = "claude-00f765af-user@example.com.json"
    ref = store.bind_oauth_credential("src_identity001", "anthropic", old_name)
    prefix = store.credential_metadata(ref)["prefix"]
    identity = {
        "type": "claude",
        "prefix": prefix,
        "email": "user@example.com",
        "account_uuid": "account-a",
        "organization_uuid": "organization-a",
        "access_token": "private-access-fixture",
    }
    store.write_oauth_auth_file(old_name, identity)
    store.reconcile_oauth_auth_file(old_name, auth_provider="claude")
    (store.auth_dir / old_name).rename(store.auth_dir / new_name)

    assert store.reconcile_oauth_auth_file(new_name, auth_provider="claude") == ref
    with pytest.raises(EngineStateError, match="already bound"):
        store.bind_oauth_credential(
            "src_other001",
            "anthropic",
            new_name,
            identity=identity,
        )
    assert store.credential_metadata(ref)["auth_name"] == new_name
    assert len(store._oauth_credentials()) == 1


def test_claude_filename_migration_rejects_identity_match_with_foreign_prefix(tmp_path):
    store = EngineStateStore(tmp_path / "engine")
    old_name = "claude-user@example.com.json"
    new_name = "claude-00f765af-user@example.com.json"
    ref = store.bind_oauth_credential("src_identity001", "anthropic", old_name)
    prefix = store.credential_metadata(ref)["prefix"]
    identity = {
        "type": "claude",
        "prefix": prefix,
        "email": "user@example.com",
        "account_uuid": "account-a",
        "organization_uuid": "organization-a",
        "access_token": "private-access-fixture",
    }
    store.write_oauth_auth_file(old_name, identity)
    store.reconcile_oauth_auth_file(old_name, auth_provider="claude")
    (store.auth_dir / old_name).rename(store.auth_dir / new_name)
    store.write_oauth_auth_file(
        new_name,
        {**identity, "prefix": "foreign-prefix"},
    )

    with pytest.raises(EngineStateError, match="prefix conflicts"):
        store.reconcile_oauth_auth_file(new_name, auth_provider="claude")

    stored = store.credential_metadata(ref)
    assert stored["auth_name"] == old_name
    assert stored["prefix"] == prefix


def test_claude_credential_reconciliation_scans_renamed_auth_file(tmp_path):
    store = EngineStateStore(tmp_path / "engine")
    old_name = "claude-user@example.com.json"
    new_name = "claude-00f765af-user@example.com.json"
    ref = store.bind_oauth_credential("src_identity001", "anthropic", old_name)
    prefix = store.credential_metadata(ref)["prefix"]
    store.write_oauth_auth_file(
        old_name,
        {
            "type": "claude",
            "prefix": prefix,
            "email": "user@example.com",
            "account_uuid": "account-a",
            "organization_uuid": "organization-a",
            "access_token": "private-access-fixture",
        },
    )
    (store.auth_dir / old_name).rename(store.auth_dir / new_name)

    assert store.reconcile_oauth_credential(ref, auth_provider="claude") == new_name
    assert store.credential_metadata(ref)["auth_name"] == new_name


def test_claude_credential_reconciliation_fails_closed_on_duplicate_migrated_files(tmp_path):
    store = EngineStateStore(tmp_path / "engine")
    old_name = "claude-user@example.com.json"
    new_name = "claude-00f765af-user@example.com.json"
    ref = store.bind_oauth_credential("src_identity001", "anthropic", old_name)
    prefix = store.credential_metadata(ref)["prefix"]
    identity = {
        "type": "claude",
        "prefix": prefix,
        "email": "user@example.com",
        "account_uuid": "account-a",
        "organization_uuid": "organization-a",
        "access_token": "private-access-fixture",
    }
    store.write_oauth_auth_file(old_name, identity)
    store.write_oauth_auth_file(new_name, identity)
    before = store.credential_metadata(ref)

    with pytest.raises(EngineStateError, match="binding is ambiguous"):
        store.reconcile_oauth_credential(ref, auth_provider="claude")

    assert store.credential_metadata(ref) == before
    assert (store.auth_dir / old_name).is_file()
    assert (store.auth_dir / new_name).is_file()


def test_claude_identity_requires_account_level_evidence(tmp_path):
    store = EngineStateStore(tmp_path / "engine")
    organization_only = {
        "type": "claude",
        "organization_uuid": "organization-a",
    }
    first_ref = store.bind_oauth_credential(
        "src_identity001",
        "anthropic",
        "claude-first.json",
        identity=organization_only,
    )

    assert store.oauth_credential_ref_for_identity("anthropic", organization_only) is None
    assert "oauth_identity" not in store.credential_metadata(first_ref)

    second_ref = store.bind_oauth_credential(
        "src_identity002",
        "anthropic",
        "claude-second.json",
        identity=organization_only,
    )
    assert second_ref != first_ref


def test_claude_exact_filename_rewrite_rejects_identity_change(tmp_path):
    store = EngineStateStore(tmp_path / "engine")
    auth_name = "claude-user@example.com.json"
    ref = store.bind_oauth_credential("src_identity001", "anthropic", auth_name)
    prefix = store.credential_metadata(ref)["prefix"]
    store.write_oauth_auth_file(
        auth_name,
        {
            "type": "claude",
            "prefix": prefix,
            "email": "user@example.com",
            "account_uuid": "account-a",
            "organization_uuid": "organization-a",
            "access_token": "private-access-fixture",
        },
    )
    store.reconcile_oauth_auth_file(auth_name, auth_provider="claude")
    store.write_oauth_auth_file(
        auth_name,
        {
            "type": "claude",
            "prefix": prefix,
            "email": "user@example.com",
            "account_uuid": "account-a",
            "organization_uuid": "organization-b",
            "access_token": "private-access-fixture",
        },
    )

    with pytest.raises(EngineStateError, match="identity conflicts"):
        store.reconcile_oauth_auth_file(auth_name, auth_provider="claude")

    stored = store.credential_metadata(ref)
    assert stored["oauth_identity"]["organization_uuid"] == "organization-a"


@pytest.mark.parametrize("renamed", [False, True], ids=["exact-name", "renamed"])
@pytest.mark.parametrize(
    "missing_field",
    ["email", "account_uuid", "organization_uuid"],
)
def test_claude_reconciliation_rejects_dropped_identity(tmp_path, renamed, missing_field):
    store = EngineStateStore(tmp_path / "engine")
    old_name = "claude-user@example.com.json"
    new_name = "claude-00f765af-user@example.com.json"
    ref = store.bind_oauth_credential("src_identity001", "anthropic", old_name)
    prefix = store.credential_metadata(ref)["prefix"]
    identity = {
        "type": "claude",
        "prefix": prefix,
        "email": "user@example.com",
        "account_uuid": "account-a",
        "organization_uuid": "organization-a",
        "access_token": "private-access-fixture",
    }
    store.write_oauth_auth_file(old_name, identity)
    store.reconcile_oauth_auth_file(old_name, auth_provider="claude")

    current_name = new_name if renamed else old_name
    if renamed:
        (store.auth_dir / old_name).rename(store.auth_dir / new_name)
    replacement = {
        "type": "claude",
        "prefix": prefix,
        "email": "user@example.com",
        "account_uuid": "account-a",
        "organization_uuid": "organization-a",
        "access_token": "private-access-fixture",
    }
    replacement.pop(missing_field)
    store.write_oauth_auth_file(current_name, replacement)

    with pytest.raises(EngineStateError, match="identity conflicts"):
        store.reconcile_oauth_auth_file(current_name, auth_provider="claude")

    stored = store.credential_metadata(ref)
    assert stored["auth_name"] == old_name
    assert stored["oauth_identity"]["organization_uuid"] == "organization-a"


@pytest.mark.parametrize("replacement_prefix", ["foreign-prefix", None])
def test_claude_exact_filename_rewrite_rejects_prefix_change(tmp_path, replacement_prefix):
    store = EngineStateStore(tmp_path / "engine")
    auth_name = "claude-user@example.com.json"
    ref = store.bind_oauth_credential("src_identity001", "anthropic", auth_name)
    prefix = store.credential_metadata(ref)["prefix"]
    identity = {
        "type": "claude",
        "prefix": prefix,
        "email": "user@example.com",
        "account_uuid": "account-a",
        "organization_uuid": "organization-a",
        "access_token": "private-access-fixture",
    }
    store.write_oauth_auth_file(auth_name, identity)
    store.reconcile_oauth_auth_file(auth_name, auth_provider="claude")
    replacement = {**identity, "prefix": replacement_prefix}
    store.write_oauth_auth_file(auth_name, replacement)

    with pytest.raises(EngineStateError, match="prefix conflicts"):
        store.reconcile_oauth_auth_file(auth_name, auth_provider="claude")

    stored = store.credential_metadata(ref)
    assert stored["auth_name"] == auth_name
    assert stored["prefix"] == prefix
    assert stored["oauth_identity"]["organization_uuid"] == "organization-a"


def test_claude_filename_migration_does_not_merge_different_organizations(tmp_path):
    store = EngineStateStore(tmp_path / "engine")
    old_name = "claude-user@example.com.json"
    ref = store.bind_oauth_credential("src_identity001", "anthropic", old_name)
    old_prefix = store.credential_metadata(ref)["prefix"]
    old_identity = {
        "type": "claude",
        "prefix": old_prefix,
        "email": "user@example.com",
        "account_uuid": "shared-account",
        "organization_uuid": "organization-a",
        "access_token": "private-access-fixture",
    }
    store.write_oauth_auth_file(old_name, old_identity)
    store.reconcile_oauth_auth_file(old_name, auth_provider="claude")

    new_name = "claude-50d86b12-user@example.com.json"
    new_identity = {
        **old_identity,
        "prefix": "foreign-prefix",
        "organization_uuid": "organization-b",
    }
    store.write_oauth_auth_file(new_name, new_identity)
    assert store.reconcile_oauth_auth_file(new_name, auth_provider="claude") is None

    new_ref = store.bind_oauth_credential(
        "src_other001",
        "anthropic",
        new_name,
        identity=new_identity,
    )
    assert new_ref != ref
    assert store.credential_metadata(ref)["auth_name"] == old_name


def test_kimi_device_metadata_is_not_presented_as_an_account(tmp_path):
    _, adapter, source_id, ref, _ = _bound_account(
        tmp_path, "kimi", device_id="device-fixture", scope="user",
    )
    assert adapter.subscription_account_label(source_id, "kimi", ref) is None


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ({"email": " person@example.com "}, "person@example.com"),
        ({"username": "小明"}, "小明"),
        ({"email": None, "username": "fixture-user"}, "fixture-user"),
        ({"email": {"nested": "person@example.com"}, "username": "fixture-user"}, "fixture-user"),
        ({"account_id": "account-123", "name": "private-access-fixture"}, None),
        ({"email": "not-an-email"}, None),
        ({"email": "line\nbreak@example.com"}, None),
        ({"email": "\ud800@example.com"}, None),
        ({"email": "\udfff@example.com"}, None),
        ({"email": "\ud800@example.com", "username": "safe-user"}, "safe-user"),
        ({"username": "\udfff"}, None),
        ({"username": "用户😀"}, "用户😀"),
        ({"username": "Bearer abcdefghijklmnop"}, None),
        ({"username": "sk-abcdefghijk123"}, None),
        ({"username": "private-access-fixture"}, None),
        ({"email": "x" * 255 + "@example.com"}, None),
        ({"email": "", "username": ""}, None),
        ({}, None),
    ],
)
def test_only_safe_public_account_fields_are_projected(tmp_path, identity, expected):
    _, adapter, source_id, ref, _ = _bound_account(tmp_path, **identity)
    assert adapter.subscription_account_label(source_id, "openai", ref) == expected


@pytest.mark.parametrize("token_field", ["access_token", "refresh_token", "id_token"])
@pytest.mark.parametrize("label_field", ["email", "username"])
def test_account_fields_cannot_publish_whitespace_wrapped_tokens(tmp_path, token_field, label_field):
    identity = {token_field: " \topaque@example.com\n ", label_field: "opaque@example.com"}
    _, adapter, source_id, ref, _ = _bound_account(tmp_path, **identity)
    assert adapter.subscription_account_label(source_id, "openai", ref) is None


@pytest.mark.parametrize("failure", ["prefix", "provider", "missing", "corrupt", "file_mode", "directory_mode", "symlink"])
def test_unavailable_or_foreign_account_metadata_is_absent_and_never_repaired(tmp_path, failure):
    store, adapter, source_id, ref, payload = _bound_account(tmp_path, email="owner@example.com")
    path = store.auth_dir / "account.json"
    if failure in {"prefix", "provider"}:
        payload["prefix" if failure == "prefix" else "type"] = "foreign"
        store.write_oauth_auth_file("account.json", payload)
    elif failure == "missing":
        path.unlink()
    elif failure == "corrupt":
        path.write_text("{")
    elif failure == "file_mode":
        path.chmod(0o644)
    elif failure == "directory_mode":
        store.auth_dir.chmod(0o755)
    elif failure == "symlink":
        target = tmp_path / "foreign.json"
        path.rename(target)
        path.symlink_to(target)
    assert adapter.subscription_account_label(source_id, "openai", ref) is None
    if failure == "file_mode":
        assert path.stat().st_mode & 0o777 == 0o644
    if failure == "directory_mode":
        assert store.auth_dir.stat().st_mode & 0o777 == 0o755
    if failure == "symlink":
        assert path.is_symlink()


def test_missing_engine_metadata_does_not_create_state_directories(tmp_path):
    store = EngineStateStore(tmp_path / "absent")
    adapter = CLIProxyEngineAdapter(supervisor=object(), state_store=store)
    assert adapter.subscription_account_label("src_identity001", "openai", "cred_missing01") is None
    assert not store.root.exists()


def test_existing_source_and_creation_result_use_current_bound_account_without_persisting_secrets(tmp_path):
    async def scenario():
        service, config_store, fake = _service(tmp_path)
        flow = await _completed_flow(service, fake, "openai")
        original = (await service.oauth_status(flow["flow_id"]))["source"]
        source = config_store.config.sources[0]
        store = EngineStateStore(tmp_path / "engine")
        source.credential_ref = store.bind_oauth_credential(source.id, "openai", "current.json")
        source.account_label = "stale@example.com"
        metadata = store.credential_metadata(source.credential_ref)
        runtime = CLIProxyEngineAdapter(supervisor=object(), state_store=store)
        fake.subscription_account_label = runtime.subscription_account_label
        payload = {
            "type": "codex", "prefix": metadata["prefix"], "email": "current@example.com",
            "access_token": "private-access-fixture",
        }
        store.write_oauth_auth_file("current.json", payload)
        saved = json.dumps(config_store.config.to_payload())
        listed = service.list_sources()[0]
        assert listed["id"] == original["id"]
        assert listed["account_label"] == "current@example.com"
        assert service._source_creation_result(source.to_payload())["source"] == listed
        assert "private-access-fixture" not in json.dumps(listed)
        payload.pop("email")
        store.write_oauth_auth_file("current.json", payload)
        assert service.list_sources()[0]["account_label"] is None
        payload["email"] = "new-account@example.com"
        store.write_oauth_auth_file("current.json", payload)
        assert service.list_sources()[0]["account_label"] == "new-account@example.com"
        assert json.dumps(config_store.config.to_payload()) == saved

    asyncio.run(scenario())
