from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from config.v2_config import (
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub.adapter import (
    DiscoveredModel,
    ObservationDiscovery,
    ObservationOutcome,
    SourceObservation,
)
from core.handlers.model_hub.events import BoundedEventLog
from core.handlers.model_hub.migration import scan_native_configs
from core.handlers.model_hub.oauth import OAuthFlowRegistry
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import (
    ModelHubError,
    ModelHubService,
    _mask_credential,
    create_default_service,
)
from core.services.settings import default_config
from tests.ui_server_test_helpers import csrf_headers
from vibe import ui_server
from vibe.ui_server import app

CONTRACTS = Path("docs/plans/model-hub-contracts")


@pytest.fixture(autouse=True)
def _enable_model_hub(monkeypatch):
    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")


class MemoryStore:
    def __init__(self) -> None:
        self.config = ModelHubConfig(
            agents={
                backend: ModelHubAgentSupplyConfig.default(backend, mode="hub")
                for backend in ("claude", "codex", "opencode")
            }
        )

    def load(self) -> ModelHubConfig:
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        self.config = config


class MigrationAdapter:
    def __init__(self) -> None:
        self.provisioned: list[tuple[str, int, str]] = []
        self.revoked: list[str] = []
        self.transient_refs: list[str] = []
        self.transient_revoked: list[str] = []
        self.observed: list[tuple[str, tuple[str, ...]]] = []
        self.observed_protocols = {
            "anthropic": "anthropic",
            "openai": "openai_responses",
            "openrouter": "openai_chat",
            "zhipuai": "openai_chat",
        }
        self.unproven_observation_vendor: str | None = None
        self.synced: list[tuple[object, ...]] = []
        self.fail_revoke_refs: set[str] = set()
        self.fail_sync_count = 0

    async def provision_transient_credential(
        self,
        vendor: str,
        secret: str,
        base_url: str | None,
    ) -> str:
        credential_ref = f"cred_observation_{len(self.transient_refs) + 1}"
        self.transient_refs.append(credential_ref)
        return credential_ref

    async def observe_source(
        self,
        vendor: str,
        base_url: str | None,
        credential_ref: str,
        protocol_order,
    ) -> SourceObservation:
        self.observed.append((vendor, tuple(protocol_order)))
        if vendor == self.unproven_observation_vendor:
            return SourceObservation(
                outcome=ObservationOutcome.AMBIGUOUS,
                reachable=True,
                authenticated=True,
                protocol=None,
                discovery=ObservationDiscovery.NOT_ATTEMPTED,
                models=(),
            )
        return SourceObservation(
            outcome=ObservationOutcome.OBSERVED,
            reachable=True,
            authenticated=True,
            protocol=self.observed_protocols[vendor],
            discovery=ObservationDiscovery.SUCCEEDED,
            models=(DiscoveredModel(id=f"{vendor}-model"),),
        )

    async def provision_credential(
        self,
        vendor: str,
        protocol: str,
        secret: str,
        base_url: str | None,
    ) -> str:
        credential_ref = f"cred_migration_{len(self.provisioned) + 1}"
        self.provisioned.append((vendor, len(secret), credential_ref))
        return credential_ref

    async def discover_models(
        self,
        vendor: str,
        protocol: str,
        base_url: str | None,
        credential_ref: str,
    ) -> tuple[DiscoveredModel, ...]:
        return (DiscoveredModel(id=f"{vendor}-model"),)

    async def sync_sources(self, bindings) -> None:
        self.synced.append(tuple(bindings))
        if self.fail_sync_count:
            self.fail_sync_count -= 1
            raise RuntimeError("redacted sync failure")

    async def revoke_credential(self, credential_ref: str) -> None:
        if credential_ref in self.fail_revoke_refs:
            raise RuntimeError("redacted revoke failure")
        if credential_ref in self.transient_refs:
            self.transient_revoked.append(credential_ref)
            return
        self.revoked.append(credential_ref)


def _service(tmp_path: Path) -> tuple[ModelHubService, MemoryStore, MigrationAdapter]:
    store = MemoryStore()
    adapter = MigrationAdapter()
    state = tmp_path / "avibe-state"
    service = ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(state / "events.json"),
        oauth_flows=OAuthFlowRegistry(state / "oauth.json"),
        revocations=CredentialRevocationJournal(state / "revocations.json"),
        now=lambda: datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc),
    )
    return service, store, adapter


def _isolate_native_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setattr(Path, "home", lambda: home)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _assert_canonical_round_trip(config: ModelHubConfig) -> ModelHubConfig:
    serialized = json.dumps(config.to_payload())
    reloaded = ModelHubConfig.from_payload(json.loads(serialized))
    assert reloaded.to_payload() == config.to_payload()
    return reloaded


def _assert_sources_validated_before_commit(
    monkeypatch: pytest.MonkeyPatch,
    service: ModelHubService,
) -> None:
    original_validator = ModelHubSourceConfig.from_payload.__func__
    validated_source_ids: set[str] = set()

    # Pass-through on purpose: what this double observes is which sources were
    # validated, so it must not pin the validator's signature. Spelling out today's
    # arguments made a policy flag the owner grew into a migration conflict.
    def tracked_validator(cls, *args, **kwargs):
        source = original_validator(cls, *args, **kwargs)
        validated_source_ids.add(source.id)
        return source

    original_commit = service._commit_synced

    async def checked_commit(previous, updated):
        new_source_ids = {
            source.id for source in updated.sources
        } - {source.id for source in previous.sources}
        assert new_source_ids <= validated_source_ids
        await original_commit(previous, updated)

    monkeypatch.setattr(
        ModelHubSourceConfig,
        "from_payload",
        classmethod(tracked_validator),
    )
    monkeypatch.setattr(service, "_commit_synced", checked_commit)


def _write_claude(home: Path, *, malformed: bool = False) -> None:
    if malformed:
        _write(home / ".claude" / "settings.json", "{not-json")
        _write(home / ".claude" / ".credentials.json", "[]")
        return
    _write(
        home / ".claude" / "settings.json",
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_API_KEY": "sk-ant-test-123456789",
                    "ANTHROPIC_BASE_URL": "https://anthropic.example/v1",
                },
                "permissions": {"allow": ["Read"]},
            },
            indent=2,
        )
        + "\n",
    )
    _write(
        home / ".claude" / ".credentials.json",
        json.dumps({"claudeAiOauth": {"accessToken": "claude-oauth-token"}}),
    )


def _write_claude_oauth(home: Path) -> None:
    _write(
        home / ".claude" / ".credentials.json",
        json.dumps({"claudeAiOauth": {"accessToken": "claude-oauth-token"}}),
    )


def _write_codex(home: Path, *, malformed: bool = False) -> None:
    _write(
        home / ".codex" / "auth.json",
        (
            "{broken"
            if malformed
            else json.dumps(
                {
                    "OPENAI_API_KEY": "sk-openai-test-123456",
                    "tokens": {"access_token": "codex-access-123456"},
                }
            )
        ),
    )
    if not malformed:
        _write(
            home / ".codex" / "config.toml",
            """cli_auth_credentials_store = "file"
model_provider = "Relay"

[model_providers.Relay]
base_url = "https://codex-relay.example/v1"
wire_api = "chat"
""",
        )


def _write_codex_oauth(home: Path) -> None:
    _write(
        home / ".codex" / "auth.json",
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": "codex-access-123456"},
            }
        ),
    )
    _write(
        home / ".codex" / "config.toml",
        'cli_auth_credentials_store = "file"\n',
    )


def _write_opencode(home: Path, *, malformed: bool = False) -> None:
    if malformed:
        _write(home / ".config" / "opencode" / "opencode.json", "{/*")
        _write(home / ".local" / "share" / "opencode" / "auth.json", "[]")
        return
    _write(
        home / ".config" / "opencode" / "opencode.json",
        """{
  // JSONC is part of the native OpenCode format.
  "provider": {
    "openrouter": {
      "options": {
        "apiKey": "{env:OPENROUTER_API_KEY}",
      },
      "models": {
        "manual-openrouter-model": {"name": "Manual OpenRouter Model"},
      },
    },
    "zhipuai": {
      "options": {
        "baseURL": "https://zhipu.example/v1",
      },
    },
  },
}
""",
    )
    _write(
        home / ".local" / "share" / "opencode" / "auth.json",
        json.dumps(
            {
                "openrouter": {"type": "api", "key": "sk-openrouter-123456"},
                "zhipuai": {"type": "api", "key": "sk-zhipu-123456"},
            }
        ),
    )
    _write(
        home / ".cache" / "opencode" / "models.json",
        json.dumps(
            {
                "openrouter": {
                    "id": "openrouter",
                    "npm": "@openrouter/ai-sdk-provider",
                    "api": "https://openrouter.ai/api/v1",
                },
                "zhipuai": {
                    "id": "zhipuai",
                    "npm": "@ai-sdk/openai-compatible",
                    "api": "https://zhipu.example/v1",
                },
            }
        ),
    )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_scan(payload: dict) -> None:
    schema = json.loads((CONTRACTS / "migration-scan.schema.json").read_text(encoding="utf-8"))
    Draft7Validator(schema).validate(payload)


def _translation_value(payload: dict, key: str) -> object:
    value: object = payload
    for part in key.split("."):
        assert isinstance(value, dict)
        value = value[part]
    return value


def test_native_config_parsers_cover_valid_malformed_and_absent(tmp_path: Path) -> None:
    cases = (
        ("claude", _write_claude, 1),
        ("codex", _write_codex, 1),
        ("opencode", _write_opencode, 2),
    )
    for backend, writer, expected in cases:
        valid_home = tmp_path / f"{backend}-valid"
        writer(valid_home)
        valid = scan_native_configs(
            ModelHubConfig(),
            home=valid_home,
            mask_credential=_mask_credential,
        )
        assert len(valid) == expected
        assert {item.backend for item in valid} == {backend}
        payload = {"items": [item.to_payload() for item in valid]}
        _validate_scan(payload)
        assert all(" + " not in item.masked_detail for item in valid)
        serialized = json.dumps(payload)
        assert "Claude OAuth" not in serialized
        assert "Codex auth.json" not in serialized
        for secret in (
            "sk-ant-test-123456789",
            "claude-oauth-token",
            "codex-access-123456",
            "sk-openai-test-123456",
            "sk-openrouter-123456",
            "sk-zhipu-123456",
        ):
            assert secret not in serialized

        malformed_home = tmp_path / f"{backend}-malformed"
        writer(malformed_home, malformed=True)
        assert (
            scan_native_configs(
                ModelHubConfig(),
                home=malformed_home,
                mask_credential=_mask_credential,
            )
            == []
        )

        absent_home = tmp_path / f"{backend}-absent"
        absent = scan_native_configs(
            ModelHubConfig(),
            home=absent_home,
            mask_credential=_mask_credential,
        )
        assert absent == []

    keychain_home = tmp_path / "claude-keychain"
    keychain_items = scan_native_configs(
        ModelHubConfig(),
        home=keychain_home,
        mask_credential=_mask_credential,
        claude_oauth_probe=lambda: True,
    )
    assert [(item.backend, item.kind, item.proposed_action) for item in keychain_items] == [
        ("claude", "oauth_native", "keep_native")
    ]


def test_claude_settings_env_suppresses_stale_oauth_store(tmp_path: Path) -> None:
    native_home = tmp_path / "native-home"
    _write_claude(native_home)

    items = scan_native_configs(
        ModelHubConfig(),
        home=native_home,
        mask_credential=_mask_credential,
    )

    assert [(item.kind, item.proposed_action) for item in items] == [
        ("api_key", "import")
    ]


@pytest.mark.parametrize(
    ("auth_mode", "expected_kind"),
    (("chatgpt", "oauth_native"), ("apikey", "api_key")),
)
def test_codex_scan_respects_explicit_auth_mode(
    tmp_path: Path,
    auth_mode: str,
    expected_kind: str,
) -> None:
    native_home = tmp_path / auth_mode
    _write_codex(native_home)
    auth_path = native_home / ".codex" / "auth.json"
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    auth["auth_mode"] = auth_mode
    auth_path.write_text(json.dumps(auth), encoding="utf-8")

    items = scan_native_configs(
        ModelHubConfig(),
        home=native_home,
        mask_credential=_mask_credential,
    )

    assert [item.kind for item in items] == [expected_kind]


def test_codex_scan_treats_tokens_as_stale_when_key_has_no_auth_mode(
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write_codex(native_home)

    items = scan_native_configs(
        ModelHubConfig(),
        home=native_home,
        mask_credential=_mask_credential,
    )

    assert [item.kind for item in items] == ["api_key"]


@pytest.mark.parametrize("credential_store", (None, "keyring"))
def test_codex_scan_keeps_native_oauth_but_skips_key_outside_file_store(
    tmp_path: Path,
    credential_store: str | None,
) -> None:
    native_home = tmp_path / (credential_store or "auto")
    _write_codex(native_home)
    config_path = native_home / ".codex" / "config.toml"
    config = config_path.read_text(encoding="utf-8")
    replacement = (
        ""
        if credential_store is None
        else f'cli_auth_credentials_store = "{credential_store}"\n'
    )
    config_path.write_text(
        config.replace('cli_auth_credentials_store = "file"\n', replacement),
        encoding="utf-8",
    )

    items = scan_native_configs(
        ModelHubConfig(),
        home=native_home,
        mask_credential=_mask_credential,
    )

    assert [(item.kind, item.proposed_action) for item in items] == [
        ("oauth_native", "keep_native")
    ]


def test_migration_note_keys_resolve_in_both_ui_locales(tmp_path: Path) -> None:
    native_home = tmp_path / "native-home"
    _write_claude(native_home)
    _write_codex(native_home)
    _write_opencode(native_home)
    note_keys = {
        item.notes_key
        for item in scan_native_configs(
            ModelHubConfig(),
            home=native_home,
            mask_credential=_mask_credential,
        )
        if item.notes_key is not None
    }

    for locale in ("en", "zh"):
        translations = json.loads(
            Path(f"ui/src/i18n/{locale}.json").read_text(encoding="utf-8")
        )
        assert all(
            isinstance(_translation_value(translations, key), str)
            for key in note_keys
        )


def test_mh_mig_001_api_apply_keeps_native_tree_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Scenario: MH-MIG-001."""

    native_home = tmp_path / "native-home"
    _write_claude(native_home)
    _write_codex(native_home)
    _write_opencode(native_home)
    _isolate_native_home(monkeypatch, native_home)
    before = _tree_digest(native_home)

    service, store, adapter = _service(tmp_path)
    _assert_sources_validated_before_commit(monkeypatch, service)
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    base_url = "http://127.0.0.1:15131"
    headers = csrf_headers(client, base_url)

    scan_response = client.post(
        "/api/models/migration/scan",
        headers=headers,
        base_url=base_url,
    )
    assert scan_response.status_code == 200
    scan = scan_response.get_json()["scan"]
    _validate_scan(scan)
    assert len(scan["items"]) == 4

    apply_response = client.post(
        "/api/models/migration/apply",
        json={"item_ids": [item["id"] for item in scan["items"]]},
        headers=headers,
        base_url=base_url,
    )
    assert apply_response.status_code == 200
    body = apply_response.get_json()
    assert body["applied"] == 4
    assert len(body["sources"]) == 4
    assert len(store.config.sources) == 4
    assert len(adapter.provisioned) == 4
    assert adapter.revoked == []
    assert adapter.transient_revoked == adapter.transient_refs
    assert len(adapter.observed) == len(adapter.transient_refs)
    assert before == _tree_digest(native_home)
    reloaded = _assert_canonical_round_trip(store.config)
    assert all(
        model.reasoning_efforts == []
        for source in reloaded.sources
        for model in source.models
    )
    by_id = {source.id: source for source in store.config.sources}
    imported_ids = set(by_id)
    for backend in ("claude", "codex", "opencode"):
        assert store.config.agents[backend].sources.order == [
            source.id
            for source in store.config.sources
            if ModelHubConfig.source_eligible_for_backend(source, backend)
        ]
    codex_payload = next(
        agent for agent in service.list_agents() if agent["backend"] == "codex"
    )
    assert codex_payload["sources"]["order"] == store.config.agents["codex"].sources.order
    assert {
        item["source_id"]
        for item in codex_payload["sources"]["eligibility"]
        if item["eligible"]
    } == imported_ids
    assert all(
        source.billing == "metered"
        for source in store.config.sources
        if source.kind == "api_key"
    )
    codex_source = next(
        source for source in store.config.sources if source.vendor == "openai" and source.kind == "api_key"
    )
    assert codex_source.protocol == "openai_responses"
    assert codex_source.base_url == "https://codex-relay.example/v1"
    openrouter_source = next(source for source in store.config.sources if source.vendor == "openrouter")
    assert openrouter_source.base_url == "https://openrouter.ai/api/v1"
    assert openrouter_source.masked_credential == _mask_credential("sk-openrouter-123456")
    assert any(
        model.id == "manual-openrouter-model"
        and model.display_name == "Manual OpenRouter Model"
        and model.provenance == "manual"
        for model in openrouter_source.models
    )

    serialized = json.dumps(body)
    for secret in (
        "sk-ant-test-123456789",
        "claude-oauth-token",
        "codex-access-123456",
        "sk-openai-test-123456",
        "sk-openrouter-123456",
        "sk-zhipu-123456",
    ):
        assert secret not in serialized


def test_mh_mig_002_oauth_defaults_to_native_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Scenario: MH-MIG-002."""

    native_home = tmp_path / "native-home"
    _write_claude_oauth(native_home)
    _write_codex_oauth(native_home)
    _isolate_native_home(monkeypatch, native_home)
    service, store, adapter = _service(tmp_path)
    _assert_sources_validated_before_commit(monkeypatch, service)

    scan = service.migration_scan()["items"]
    oauth_items = [item for item in scan if item["kind"] == "oauth_native"]
    assert {item["backend"] for item in oauth_items} == {"claude", "codex"}
    assert {item["proposed_action"] for item in oauth_items} == {"keep_native"}

    result = asyncio.run(service.migration_apply([item["id"] for item in oauth_items]))
    assert result["applied"] == 2
    assert {position["source_id"] for position in result["added_to"]} == {
        source.id for source in store.config.sources
    }
    assert {
        (source.vendor, source.kind, source.supply_channel, source.credential_ref) for source in store.config.sources
    } == {
        ("anthropic", "subscription", "native_cli", None),
        ("openai", "subscription", "native_cli", None),
    }
    assert adapter.provisioned == []
    from vibe.backend_model_catalog import (
        bundled_catalog_reasoning_efforts_for_model,
    )

    for source in store.config.sources:
        assert source.models
        for model in source.models:
            assert model.reasoning_efforts_source == "catalog"
            assert tuple(model.reasoning_efforts) == (
                bundled_catalog_reasoning_efforts_for_model(model.id)
            )
    reloaded = _assert_canonical_round_trip(store.config)
    assert {
        (source.vendor, source.kind, source.supply_channel, source.credential_ref)
        for source in reloaded.sources
    } == {
        ("anthropic", "subscription", "native_cli", None),
        ("openai", "subscription", "native_cli", None),
    }


@pytest.mark.parametrize(
    ("writer", "vendor", "protocol"),
    (
        (_write_claude_oauth, "anthropic", "anthropic"),
        (_write_codex_oauth, "openai", "openai_responses"),
    ),
)
def test_scan_suppresses_existing_native_subscription_semantically(
    tmp_path: Path,
    writer,
    vendor: str,
    protocol: str,
) -> None:
    native_home = tmp_path / vendor
    writer(native_home)
    config = ModelHubConfig(
        sources=[
            ModelHubSourceConfig(
                id="src_existing_native",
                kind="subscription",
                vendor=vendor,
                display_name="Existing native",
                protocol=protocol,
                supply_channel="native_cli",
                billing="monthly",
                state=ModelHubSourceStateConfig(),
                models=[],
            )
        ]
    )

    assert (
        scan_native_configs(
            config,
            home=native_home,
            mask_credential=_mask_credential,
        )
        == []
    )


def test_opencode_auth_only_custom_provider_without_base_url_is_not_importable(
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write(
        native_home / ".local" / "share" / "opencode" / "auth.json",
        json.dumps({"custom-provider": {"type": "api", "key": "sk-custom-123456"}}),
    )

    assert (
        scan_native_configs(
            ModelHubConfig(),
            home=native_home,
            mask_credential=_mask_credential,
        )
        == []
    )


def test_claude_auth_token_requires_reauth_without_changing_header_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write(
        native_home / ".claude" / "settings.json",
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": "bearer-test-123456",
                    "ANTHROPIC_BASE_URL": "https://bearer-relay.example/v1",
                }
            }
        ),
    )
    _isolate_native_home(monkeypatch, native_home)
    service, store, adapter = _service(tmp_path)

    [item] = service.migration_scan()["items"]
    assert item["proposed_action"] == "reauth"
    assert item["selected"] is False
    assert item["notes_key"] == "settings.models.source.customEndpoint"
    assert "bearer-test-123456" not in json.dumps(item)

    with pytest.raises(ModelHubError) as error:
        asyncio.run(service.migration_apply([item["id"]]))
    assert error.value.code == "migration_item_conflict"
    assert adapter.provisioned == []
    assert store.config.sources == []


def test_codex_empty_token_bag_does_not_create_native_subscription(tmp_path: Path) -> None:
    native_home = tmp_path / "native-home"
    _write(native_home / ".codex" / "auth.json", json.dumps({"tokens": {}}))
    _write(
        native_home / ".codex" / "config.toml",
        'cli_auth_credentials_store = "file"\n',
    )

    assert (
        scan_native_configs(
            ModelHubConfig(),
            home=native_home,
            mask_credential=_mask_credential,
        )
        == []
    )


def test_codex_wire_protocol_change_invalidates_scanned_item(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write_codex(native_home)
    _isolate_native_home(monkeypatch, native_home)
    service, store, adapter = _service(tmp_path)
    item_id = next(
        item["id"]
        for item in service.migration_scan()["items"]
        if item["kind"] == "api_key"
    )
    config_path = native_home / ".codex" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'wire_api = "chat"',
            'wire_api = "responses"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ModelHubError) as error:
        asyncio.run(service.migration_apply([item_id]))
    assert error.value.code == "migration_item_conflict"
    assert adapter.provisioned == []
    assert store.config.sources == []


def test_opencode_unsupported_native_sdk_is_not_guessed_as_compatible(tmp_path: Path) -> None:
    native_home = tmp_path / "native-home"
    _write(
        native_home / ".config" / "opencode" / "opencode.json",
        json.dumps(
            {
                "provider": {
                    "google": {
                        "options": {
                            "apiKey": "google-test-123456",
                            "baseURL": "https://google.example/v1",
                        }
                    }
                }
            }
        ),
    )
    _write(
        native_home / ".cache" / "opencode" / "models.json",
        json.dumps({"google": {"id": "google", "npm": "@ai-sdk/google"}}),
    )

    assert (
        scan_native_configs(
            ModelHubConfig(),
            home=native_home,
            mask_credential=_mask_credential,
        )
        == []
    )


def test_opencode_builtin_provider_is_importable_without_catalog_cache(
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write(
        native_home / ".config" / "opencode" / "opencode.json",
        json.dumps(
            {
                "provider": {
                    "openrouter": {
                        "options": {
                            "apiKey": "sk-openrouter-123456",
                            "baseURL": "https://openrouter.example/v1",
                        }
                    }
                }
            }
        ),
    )

    [item] = scan_native_configs(
        ModelHubConfig(),
        home=native_home,
        mask_credential=_mask_credential,
    )

    assert item.vendor == "openrouter"
    assert item.protocol == "openai_chat"
    assert item.base_url == "https://openrouter.example/v1"


@pytest.mark.parametrize("provider_id", ("alibaba-cn", "poe"))
def test_opencode_native_provider_without_stable_identifier_is_not_imported(
    tmp_path: Path,
    provider_id: str,
) -> None:
    native_home = tmp_path / provider_id
    _write(
        native_home / ".config" / "opencode" / "opencode.json",
        json.dumps(
            {
                "provider": {
                    provider_id: {
                        "options": {
                            "apiKey": "provider-test-123456",
                            "baseURL": "https://provider.example/v1",
                        }
                    }
                }
            }
        ),
    )
    _write(
        native_home / ".cache" / "opencode" / "models.json",
        json.dumps(
            {
                provider_id: {
                    "npm": "@ai-sdk/openai-compatible",
                    "api": "https://provider.example/v1",
                }
            }
        ),
    )

    assert (
        scan_native_configs(
            ModelHubConfig(),
            home=native_home,
            mask_credential=_mask_credential,
        )
        == []
    )


def test_scan_skips_invalid_endpoint_without_blocking_valid_items(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write(
        native_home / ".claude" / "settings.json",
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_API_KEY": "sk-ant-test-123456789",
                    "ANTHROPIC_BASE_URL": "ftp://unsupported.example/v1",
                }
            }
        ),
    )
    _write_opencode(native_home)
    _isolate_native_home(monkeypatch, native_home)
    service, store, _ = _service(tmp_path)

    scan = service.migration_scan()["items"]
    assert {item["backend"] for item in scan} == {"opencode"}
    result = asyncio.run(service.migration_apply([item["id"] for item in scan]))
    assert result["applied"] == 2
    assert all(source.verification_pending for source in store.config.sources)
    assert {source.vendor for source in store.config.sources} == {
        "openrouter",
        "zhipuai",
    }


def test_opencode_env_placeholder_without_auth_fallback_is_not_importable(tmp_path: Path) -> None:
    native_home = tmp_path / "native-home"
    _write(
        native_home / ".config" / "opencode" / "opencode.json",
        json.dumps(
            {
                "provider": {
                    "openrouter": {
                        "options": {"apiKey": "{env:OPENROUTER_API_KEY}"}
                    }
                }
            }
        ),
    )
    _write(
        native_home / ".cache" / "opencode" / "models.json",
        json.dumps(
            {
                "openrouter": {
                    "id": "openrouter",
                    "npm": "@openrouter/ai-sdk-provider",
                    "api": "https://openrouter.ai/api/v1",
                }
            }
        ),
    )

    assert (
        scan_native_configs(
            ModelHubConfig(),
            home=native_home,
            mask_credential=_mask_credential,
        )
        == []
    )


def test_apply_scans_claude_oauth_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _isolate_native_home(monkeypatch, native_home)
    service, _, _ = _service(tmp_path)
    probe_threads: list[int] = []
    service.migration_claude_oauth_probe = lambda: (probe_threads.append(threading.get_ident()) or True)
    item_id = service.migration_scan()["items"][0]["id"]
    probe_threads.clear()
    event_loop_thread = threading.get_ident()

    asyncio.run(service.migration_apply([item_id]))

    assert probe_threads
    assert all(thread_id != event_loop_thread for thread_id in probe_threads)


def test_default_service_claude_probe_uses_safe_runtime_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _isolate_native_home(monkeypatch, native_home)
    config = default_config()
    config.runtime.default_cwd = str(tmp_path / "runtime")
    monkeypatch.setattr(
        "core.handlers.model_hub.service.V2Config.load",
        classmethod(lambda _cls: config),
    )
    monkeypatch.setattr(
        "core.handlers.model_hub.service.paths.get_state_dir",
        lambda: tmp_path / "state",
    )

    def fake_probe(_cli_path, *, env, cwd):
        assert env is not None
        assert cwd == str(tmp_path / "runtime")
        return True

    monkeypatch.setattr("vibe.api._read_claude_cli_oauth_signed_in", fake_probe)
    service = create_default_service()

    assert service.migration_claude_oauth_probe is not None
    assert service.migration_claude_oauth_probe() is True
    assert (tmp_path / "runtime").is_dir()


def test_failed_batch_revokes_every_provisioned_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write_opencode(native_home)
    _isolate_native_home(monkeypatch, native_home)
    service, store, adapter = _service(tmp_path)
    item_ids = [item["id"] for item in service.migration_scan()["items"]]
    adapter.unproven_observation_vendor = "zhipuai"

    with pytest.raises(ModelHubError) as error:
        asyncio.run(service.migration_apply(item_ids))
    assert error.value.code == "migration_item_conflict"
    assert adapter.revoked == ["cred_migration_1"]
    assert adapter.transient_revoked == adapter.transient_refs
    assert store.config.sources == []
    assert all(agent.sources.order == [] for agent in store.config.agents.values())


def test_canonical_source_rejection_aborts_batch_and_revokes_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write_opencode(native_home)
    _isolate_native_home(monkeypatch, native_home)
    service, store, adapter = _service(tmp_path)
    item_ids = [item["id"] for item in service.migration_scan()["items"]]
    original_validator = ModelHubSourceConfig.from_payload.__func__

    def reject_one_source(cls, payload):
        if payload.get("vendor") == "zhipuai":
            raise ValueError("injected canonical rejection")
        return original_validator(cls, payload)

    monkeypatch.setattr(
        ModelHubSourceConfig,
        "from_payload",
        classmethod(reject_one_source),
    )

    with pytest.raises(ModelHubError) as error:
        asyncio.run(service.migration_apply(item_ids))

    assert error.value.code == "migration_item_conflict"
    assert adapter.revoked == ["cred_migration_2", "cred_migration_1"]
    assert store.config.sources == []
    assert all(agent.sources.order == [] for agent in store.config.agents.values())


def test_failed_persist_sync_restores_config_and_revokes_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write_opencode(native_home)
    _isolate_native_home(monkeypatch, native_home)
    service, store, adapter = _service(tmp_path)
    item_ids = [item["id"] for item in service.migration_scan()["items"]]
    adapter.fail_sync_count = 1

    with pytest.raises(ModelHubError) as error:
        asyncio.run(service.migration_apply(item_ids))
    assert error.value.code == "engine_down"
    assert adapter.revoked == ["cred_migration_2", "cred_migration_1"]
    assert store.config.sources == []
    assert all(agent.sources.order == [] for agent in store.config.agents.values())


def test_failed_revoke_survives_retry_with_same_source_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write_opencode(native_home)
    _isolate_native_home(monkeypatch, native_home)
    service, store, adapter = _service(tmp_path)
    item_id = service.migration_scan()["items"][0]["id"]
    adapter.fail_sync_count = 1
    adapter.fail_revoke_refs.add("cred_migration_1")

    with pytest.raises(ModelHubError) as error:
        asyncio.run(service.migration_apply([item_id]))
    assert error.value.code == "engine_down"
    [pending] = service.revocations.list()
    assert ":migration:" in pending.source_id
    assert pending.credential_ref == "cred_migration_1"

    adapter.fail_revoke_refs.clear()
    result = asyncio.run(service.migration_apply([item_id]))
    assert result["applied"] == 1
    [active] = store.config.sources
    assert active.id != pending.source_id
    assert active.credential_ref == "cred_migration_2"

    asyncio.run(service._ensure_engine_synced())
    assert adapter.revoked == ["cred_migration_1"]
    assert service.revocations.list() == []


def test_apply_rejects_a_credential_changed_after_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write_opencode(native_home)
    _isolate_native_home(monkeypatch, native_home)
    service, store, adapter = _service(tmp_path)
    stale_id = service.migration_scan()["items"][0]["id"]
    auth_path = native_home / ".local" / "share" / "opencode" / "auth.json"
    auth_path.write_text(
        auth_path.read_text(encoding="utf-8").replace(
            "sk-openrouter-123456",
            "sk-openrouter-rotated",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ModelHubError) as error:
        asyncio.run(service.migration_apply([stale_id]))
    assert error.value.code == "migration_item_conflict"
    assert adapter.provisioned == []
    assert store.config.sources == []
