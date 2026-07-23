from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from config.v2_config import ModelHubAgentSupplyConfig, ModelHubConfig
from core.handlers.model_hub.events import BoundedEventLog
from core.handlers.model_hub.migration import scan_native_configs
from core.handlers.model_hub.oauth import OAuthFlowRegistry
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import ModelHubError, ModelHubService, _mask_credential
from tests.ui_server_test_helpers import csrf_headers
from vibe import ui_server
from vibe.ui_server import app

CONTRACTS = Path("docs/plans/model-hub-contracts")


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
        self.synced: list[tuple[object, ...]] = []
        self.fail_discovery_ref: str | None = None
        self.fail_revoke_refs: set[str] = set()
        self.fail_sync_count = 0

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
    ) -> tuple[str, ...]:
        if credential_ref == self.fail_discovery_ref:
            raise RuntimeError("redacted upstream failure")
        return (f"{vendor}-model",)

    async def sync_sources(self, bindings) -> None:
        self.synced.append(tuple(bindings))
        if self.fail_sync_count:
            self.fail_sync_count -= 1
            raise RuntimeError("redacted sync failure")

    async def revoke_credential(self, credential_ref: str) -> None:
        if credential_ref in self.fail_revoke_refs:
            raise RuntimeError("redacted revoke failure")
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
            """model_provider = "Relay"

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
    scan = scan_response.get_json()
    _validate_scan({"items": scan["items"]})
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
    assert before == _tree_digest(native_home)
    by_id = {source.id: source for source in store.config.sources}
    assert all(by_id[source_id].billing == "metered" for source_id in store.config.priority_order)
    codex_source = next(
        source for source in store.config.sources if source.vendor == "openai" and source.kind == "api_key"
    )
    assert codex_source.protocol == "openai_chat"
    assert codex_source.base_url == "https://codex-relay.example/v1"
    openrouter_source = next(source for source in store.config.sources if source.vendor == "openrouter")
    assert openrouter_source.base_url == "https://openrouter.ai/api/v1"
    assert openrouter_source.masked_credential == _mask_credential("sk-openrouter-123456")
    assert any(
        model.id == "manual-openrouter-model" and model.provenance == "manual" for model in openrouter_source.models
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

    scan = service.migration_scan()["items"]
    oauth_items = [item for item in scan if item["kind"] == "oauth_native"]
    assert {item["backend"] for item in oauth_items} == {"claude", "codex"}
    assert {item["proposed_action"] for item in oauth_items} == {"keep_native"}

    result = asyncio.run(service.migration_apply([item["id"] for item in oauth_items]))
    assert result["applied"] == 2
    assert {
        (source.vendor, source.kind, source.supply_channel, source.credential_ref) for source in store.config.sources
    } == {
        ("anthropic", "subscription", "native_cli", None),
        ("openai", "subscription", "native_cli", None),
    }
    assert adapter.provisioned == []


def test_mh_mig_003_experimental_flag_keeps_oauth_native(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Scenario: MH-MIG-003."""

    native_home = tmp_path / "native-home"
    _write_codex_oauth(native_home)
    _isolate_native_home(monkeypatch, native_home)
    service, store, adapter = _service(tmp_path)
    store.config.subscription_hub_experimental = True
    oauth_item = next(item for item in service.migration_scan()["items"] if item["kind"] == "oauth_native")
    assert oauth_item["proposed_action"] == "keep_native"
    assert oauth_item["notes_key"] == "settings.models.source.nativeSupply"

    result = asyncio.run(service.migration_apply([oauth_item["id"]]))
    assert result["applied"] == 1
    assert adapter.provisioned == []
    assert len(store.config.sources) == 1
    assert store.config.sources[0].supply_channel == "native_cli"


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
    assert item.protocol == "openai_compatible"
    assert item.base_url == "https://openrouter.example/v1"


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


def test_failed_batch_revokes_every_provisioned_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write_opencode(native_home)
    _isolate_native_home(monkeypatch, native_home)
    service, store, adapter = _service(tmp_path)
    item_ids = [item["id"] for item in service.migration_scan()["items"]]
    adapter.fail_discovery_ref = "cred_migration_2"

    with pytest.raises(ModelHubError) as error:
        asyncio.run(service.migration_apply(item_ids))
    assert error.value.code == "engine_down"
    assert adapter.revoked == ["cred_migration_2", "cred_migration_1"]
    assert store.config.sources == []
    assert store.config.priority_order == []


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
    assert store.config.priority_order == []


def test_failed_revoke_survives_retry_with_same_source_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write_opencode(native_home)
    _isolate_native_home(monkeypatch, native_home)
    service, store, adapter = _service(tmp_path)
    item_id = service.migration_scan()["items"][0]["id"]
    adapter.fail_discovery_ref = "cred_migration_1"
    adapter.fail_revoke_refs.add("cred_migration_1")

    with pytest.raises(ModelHubError) as error:
        asyncio.run(service.migration_apply([item_id]))
    assert error.value.code == "engine_down"
    [pending] = service.revocations.list()
    assert ":migration:" in pending.source_id
    assert pending.credential_ref == "cred_migration_1"

    adapter.fail_discovery_ref = None
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
