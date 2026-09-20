from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

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
    EngineEnsureResult,
    EngineHealth,
    EngineStatus,
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
)
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
        self.oauth_provisioned: list[tuple[str, str, dict[str, object]]] = []
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
        self.activated: list[str] = []
        self.validated: list[str] = []
        self.keys: dict[str, tuple[str, str, str, str | None]] = {}
        self.auth_schemes: dict[str, str | None] = {}

    async def start(self):
        return EngineStatus(EngineHealth.OK, "fixture", True, "127.0.0.1", 32199, None)

    async def ensure_installed(self, **kwargs):
        return EngineEnsureResult(await self.start(), False)

    async def activate_oauth_credential(self, credential_ref):
        self.activated.append(credential_ref)

    async def validate_oauth_credential(self, credential_ref):
        assert credential_ref in self.activated
        self.validated.append(credential_ref)

    async def matches_api_key_credential(
        self, credential_ref, vendor, protocol, secret, base_url, *, auth_scheme=None,
    ):
        return (
            self.keys.get(credential_ref) == (vendor, protocol, secret, base_url)
            and self.auth_schemes.get(credential_ref) == auth_scheme
        )

    async def credential_auth_scheme(self, credential_ref):
        return self.auth_schemes.get(credential_ref)

    async def provision_transient_credential(
        self,
        vendor: str,
        secret: str,
        base_url: str | None,
        *, on_reserved=None, auth_scheme=None,
    ) -> str:
        credential_ref = f"cred_observation_{len(self.transient_refs) + 1}"
        if on_reserved:
            on_reserved(credential_ref)
        self.transient_refs.append(credential_ref)
        self.auth_schemes[credential_ref] = auth_scheme
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
        *, on_reserved=None, auth_scheme=None,
    ) -> str:
        credential_ref = f"cred_migration_{len(self.provisioned) + 1}"
        if on_reserved:
            on_reserved(credential_ref)
        self.provisioned.append((vendor, len(secret), credential_ref))
        self.keys[credential_ref] = (vendor, protocol, secret, base_url)
        self.auth_schemes[credential_ref] = auth_scheme
        return credential_ref

    async def provision_oauth_credential(
        self,
        source_id: str,
        vendor: str,
        material: Mapping[str, object],
        *, on_reserved=None,
    ) -> str:
        credential_ref = f"cred_oauth_migration_{len(self.oauth_provisioned) + 1}"
        if on_reserved:
            on_reserved(credential_ref)
        self.oauth_provisioned.append((source_id, vendor, dict(material)))
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


def _service(
    tmp_path: Path, *, migration_home: Path | None = None,
) -> tuple[ModelHubService, MemoryStore, MigrationAdapter]:
    store = MemoryStore()
    adapter = MigrationAdapter()
    state = tmp_path / "avibe-state"

    async def verify_fixture_idle():
        return None

    service = ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(state / "events.json"),
        oauth_flows=OAuthFlowRegistry(state / "oauth.json"),
        revocations=CredentialRevocationJournal(state / "revocations.json"),
        migration_home=migration_home or tmp_path / "native-home",
        migration_guard=lambda backends: nullcontext(verify_fixture_idle),
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
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "claude-oauth-token",
                    "refreshToken": "claude-refresh-token",
                }
            }
        ),
    )


def _write_claude_oauth(home: Path) -> None:
    _write(
        home / ".claude" / ".credentials.json",
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "claude-oauth-token",
                    "refreshToken": "claude-refresh-token",
                }
            }
        ),
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
                    "tokens": {
                        "access_token": "codex-access-123456",
                        "refresh_token": "codex-refresh-123456",
                        "account_id": "acct_codex_test",
                    },
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
                "tokens": {
                    "access_token": "codex-access-123456",
                    "refresh_token": "codex-refresh-123456",
                    "account_id": "acct_codex_test",
                },
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
        ("claude", _write_claude, 2),
        ("codex", _write_codex, 2),
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
        malformed = scan_native_configs(
            ModelHubConfig(),
            home=malformed_home,
            mask_credential=_mask_credential,
        )
        assert malformed
        assert {item.backend for item in malformed} == {backend}
        assert all(
            item.proposed_action in {"keep_native", "reauth"}
            and item.selected is False
            for item in malformed
        )

        absent_home = tmp_path / f"{backend}-absent"
        absent = scan_native_configs(
            ModelHubConfig(),
            home=absent_home,
            mask_credential=_mask_credential,
        )
        assert absent == []

def test_claude_settings_and_dormant_oauth_store_are_both_visible(
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write_claude(native_home)
    _write(
        native_home / ".claude" / ".credentials.json",
        json.dumps({"claudeAiOauth": {"accessToken": "access-only-token"}}),
    )

    items = scan_native_configs(
        ModelHubConfig(),
        home=native_home,
        mask_credential=_mask_credential,
    )

    assert [(item.kind, item.proposed_action) for item in items] == [
        ("api_key", "import"),
        ("oauth_native", "keep_native"),
    ]


@pytest.mark.parametrize(
    "auth_mode",
    ("chatgpt", "apikey"),
)
def test_codex_scan_imports_dormant_auth_and_settings_keys(
    tmp_path: Path,
    auth_mode: str,
) -> None:
    native_home = tmp_path / auth_mode
    _write_codex(native_home)
    auth_path = native_home / ".codex" / "auth.json"
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    auth["auth_mode"] = auth_mode
    auth["tokens"] = {
        "access_token": "codex-access-123456",
        "refresh_token": "codex-refresh-123456",
        "account_id": "acct_codex_test",
    }
    auth_path.write_text(json.dumps(auth), encoding="utf-8")

    items = scan_native_configs(
        ModelHubConfig(),
        home=native_home,
        mask_credential=_mask_credential,
    )

    assert [(item.kind, item.proposed_action) for item in items] == [
        ("oauth_native", "import"),
        ("api_key", "import"),
    ]


def test_codex_scan_keeps_access_only_oauth_visible_alongside_api_key(
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write_codex(native_home)
    auth_path = native_home / ".codex" / "auth.json"
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    auth["tokens"] = {"access_token": "codex-access-123456"}
    auth_path.write_text(json.dumps(auth), encoding="utf-8")

    items = scan_native_configs(
        ModelHubConfig(),
        home=native_home,
        mask_credential=_mask_credential,
    )

    assert [(item.kind, item.proposed_action) for item in items] == [
        ("oauth_native", "keep_native"),
        ("api_key", "import"),
    ]


@pytest.mark.parametrize("credential_store", (None, "keyring"))
def test_codex_scan_respects_the_configured_native_store(
    tmp_path: Path,
    credential_store: str | None,
) -> None:
    native_home = tmp_path / (credential_store or "auto")
    _write_codex(native_home)
    auth_path = native_home / ".codex" / "auth.json"
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    auth["tokens"] = {"access_token": "codex-access-123456"}
    auth_path.write_text(json.dumps(auth), encoding="utf-8")
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

    expected = (
        [("oauth_native", "keep_native"), ("api_key", "import")]
        if credential_store is None
        else []
    )
    assert [(item.kind, item.proposed_action) for item in items] == expected


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


def test_mh_mig_001_api_apply_takes_over_credentials_and_cleans_native_auth(
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
    assert len(scan["items"]) == 6

    apply_response = client.post(
        "/api/models/migration/apply",
        json={"item_ids": [item["id"] for item in scan["items"]]},
        headers=headers,
        base_url=base_url,
    )
    assert apply_response.status_code == 200
    body = apply_response.get_json()
    assert body["applied"] == 6
    assert len(body["sources"]) == 6
    assert len(store.config.sources) == 6
    assert len(adapter.provisioned) == 4
    assert len(adapter.oauth_provisioned) == 2
    assert adapter.revoked == []
    assert adapter.transient_revoked == adapter.transient_refs
    assert len(adapter.observed) == len(adapter.transient_refs)
    assert before != _tree_digest(native_home)
    claude_settings = json.loads(
        (native_home / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert claude_settings["env"] == {}
    assert claude_settings["permissions"] == {"allow": ["Read"]}
    assert not (native_home / ".claude" / ".credentials.json").exists()
    assert not (native_home / ".codex" / "auth.json").exists()
    codex_config = (native_home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "\nmodel_provider =" not in f"\n{codex_config}"
    assert "[model_providers.Relay]" in codex_config
    assert 'wire_api = "chat"' in codex_config
    opencode_config = (
        native_home / ".config" / "opencode" / "opencode.json"
    ).read_text(encoding="utf-8")
    # The unset reference is an auth hook, not an unrelated preference. Its
    # auth.json fallback was migrated; no direct override should remain.
    assert "{env:OPENROUTER_API_KEY}" not in opencode_config
    reloaded = _assert_canonical_round_trip(store.config)
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
        "claude-refresh-token",
        "codex-access-123456",
        "codex-refresh-123456",
        "sk-openai-test-123456",
        "sk-openrouter-123456",
        "sk-zhipu-123456",
    ):
        assert secret not in serialized


@pytest.mark.parametrize("discovery", (ObservationDiscovery.SUCCEEDED, ObservationDiscovery.FAILED))
def test_mh_mig_001_long_manual_inventory_survives_takeover_even_when_discovery_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, discovery: ObservationDiscovery,
) -> None:
    """MH-MIG-001: new manual imports share admission without rewriting native files."""
    native_home = tmp_path / "native-home"
    _isolate_native_home(monkeypatch, native_home)
    identity = "模型🧪/e\u0301" * 3000
    invalid = (" " * 17000, "m" * 17000 + "\ud800", "m" * 17000 + " sk-fabricated-never-persist-this")
    _write(
        native_home / ".config" / "opencode" / "opencode.json",
        json.dumps({"provider": {"openrouter": {
            "options": {"apiKey": "sk-openrouter-123456", "baseURL": "https://openrouter.example/v1"},
            "models": {
                "  " + identity + "  ": {"name": "Long manual"},
                **{model_id: {} for model_id in invalid},
            },
        }}}),
    )
    before = _tree_digest(native_home)
    service, store, adapter = _service(tmp_path)
    _assert_sources_validated_before_commit(monkeypatch, service)

    async def observe(*_args):
        return SourceObservation(
            outcome=ObservationOutcome.OBSERVED, reachable=True, authenticated=True,
            protocol="openai_chat", discovery=discovery,
            models=(DiscoveredModel(id=identity + "-discovered"),) if discovery is ObservationDiscovery.SUCCEEDED else (),
        )

    adapter.observe_source = observe
    [item] = service.migration_scan()["items"]
    result = asyncio.run(service.migration_apply([item["id"]]))
    assert result["applied"] == 1
    [source] = _assert_canonical_round_trip(store.config).sources
    manual = [model for model in source.models if model.provenance == "manual"]
    assert [(model.id, model.display_name) for model in manual] == [(identity, "Long manual")]
    assert all(model.id not in invalid for model in source.models)
    assert source.state.status == "standby"
    assert _tree_digest(native_home) != before
    config_payload = json.loads(
        (native_home / ".config" / "opencode" / "opencode.json").read_text(
            encoding="utf-8"
        )
    )
    provider = config_payload["provider"]["openrouter"]
    assert "apiKey" not in provider.get("options", {})
    assert provider["models"]["  " + identity + "  "] == {"name": "Long manual"}
    assert adapter.transient_revoked == adapter.transient_refs
    assert "sk-fabricated-never-persist-this" not in json.dumps(result)


def test_mh_mig_002_oauth_grants_move_into_engine_custody(
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
    assert {item["proposed_action"] for item in oauth_items} == {"import"}

    result = asyncio.run(service.migration_apply([item["id"] for item in oauth_items]))
    assert result["applied"] == 2
    assert {position["source_id"] for position in result["added_to"]} == {
        source.id for source in store.config.sources
    }
    assert {
        (source.vendor, source.kind, source.supply_channel, bool(source.credential_ref))
        for source in store.config.sources
    } == {
        ("anthropic", "subscription", "hub", True),
        ("openai", "subscription", "hub", True),
    }
    assert adapter.provisioned == []
    assert {vendor for _source_id, vendor, _material in adapter.oauth_provisioned} == {
        "anthropic",
        "openai",
    }
    assert {
        material["refresh_token"]
        for _source_id, _vendor, material in adapter.oauth_provisioned
    } == {"claude-refresh-token", "codex-refresh-123456"}
    assert not (native_home / ".claude" / ".credentials.json").exists()
    assert not (native_home / ".codex" / "auth.json").exists()
    reloaded = _assert_canonical_round_trip(store.config)
    assert all(source.credential_ref for source in reloaded.sources)


@pytest.mark.parametrize(
    ("writer", "vendor", "protocol"),
    (
        (_write_claude_oauth, "anthropic", "anthropic"),
        (_write_codex_oauth, "openai", "openai_responses"),
    ),
)
def test_scan_reuses_existing_native_subscription_source_identity(
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

    [item] = scan_native_configs(
        config,
        home=native_home,
        mask_credential=_mask_credential,
    )
    assert item.proposed_action == "import"
    assert item.source_id == "src_existing_native"


def test_opencode_auth_only_custom_provider_without_base_url_is_visible_blocker(
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write(
        native_home / ".local" / "share" / "opencode" / "auth.json",
        json.dumps({"custom-provider": {"type": "api", "key": "sk-custom-123456"}}),
    )

    [item] = scan_native_configs(
        ModelHubConfig(),
        home=native_home,
        mask_credential=_mask_credential,
    )
    assert item.proposed_action == "reauth"
    assert item.selected is False


@pytest.mark.parametrize("base_url,token,allowed", [
    ("https://bearer-relay.example/v1", "bearer-test-123456", True),
    ("https://api.anthropic.com", "bearer-test-123456", False),
    ("https://bearer-relay.example/v1", "sk-ant-oat-fixture", False),
])
def test_claude_auth_token_preserves_supported_static_header_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    base_url: str,
    token: str,
    allowed: bool,
) -> None:
    native_home = tmp_path / "native-home"
    _write(
        native_home / ".claude" / "settings.json",
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": token,
                    "ANTHROPIC_BASE_URL": base_url,
                }
            }
        ),
    )
    _isolate_native_home(monkeypatch, native_home)
    service, store, adapter = _service(tmp_path)

    [item] = service.migration_scan()["items"]
    assert item["selected"] is allowed
    assert token not in json.dumps(item)
    if allowed:
        assert item["proposed_action"] == "import"
        result = asyncio.run(service.migration_apply([item["id"]]))
        assert result["applied"] == 1
        [source] = store.config.sources
        assert adapter.auth_schemes[source.credential_ref] == "bearer"
        assert adapter.keys[source.credential_ref][2:] == (token, base_url)
        assert service.migration_scan()["items"] == []
        return

    assert item["proposed_action"] == "reauth"
    assert item["notes_key"] == "settings.models.migration.blocked.token"

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


def test_opencode_unsupported_native_sdk_is_visible_blocker(tmp_path: Path) -> None:
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

    [item] = scan_native_configs(
        ModelHubConfig(),
        home=native_home,
        mask_credential=_mask_credential,
    )
    assert item.proposed_action == "reauth"
    assert item.selected is False


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
def test_opencode_native_provider_without_stable_identifier_is_visible_blocker(
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

    [item] = scan_native_configs(
        ModelHubConfig(),
        home=native_home,
        mask_credential=_mask_credential,
    )
    assert item.proposed_action == "reauth"
    assert item.selected is False


def test_scan_keeps_invalid_endpoint_visible_while_valid_backend_items_remain_usable(
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
    assert {item["backend"] for item in scan} == {"claude", "opencode"}
    claude_blockers = [item for item in scan if item["backend"] == "claude"]
    assert len(claude_blockers) == 1
    assert claude_blockers[0]["proposed_action"] == "reauth"
    assert claude_blockers[0]["selected"] is False
    opencode_items = [item for item in scan if item["backend"] == "opencode"]
    result = asyncio.run(service.migration_apply([item["id"] for item in opencode_items]))
    assert result["applied"] == 2
    assert {source.vendor for source in store.config.sources} == {
        "openrouter",
        "zhipuai",
    }


def test_opencode_env_placeholder_without_auth_fallback_is_visible_blocker(
    tmp_path: Path,
) -> None:
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

    [item] = scan_native_configs(
        ModelHubConfig(),
        home=native_home,
        mask_credential=_mask_credential,
    )
    assert item.proposed_action == "reauth"
    assert item.selected is False


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
    assert error.value.code == "migration_recovery_pending"
    assert store.config.sources
    assert service.migration_journal.load()["phase"] == "exposed"


def test_migration_requires_grouped_backend_selection_before_provisioning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_home = tmp_path / "native-home"
    _write_opencode(native_home)
    _isolate_native_home(monkeypatch, native_home)
    service, store, adapter = _service(tmp_path)
    item_ids = [item["id"] for item in service.migration_scan()["items"]]

    with pytest.raises(ModelHubError) as error:
        asyncio.run(service.migration_apply([item_ids[0]]))
    assert error.value.code == "migration_item_conflict"
    assert adapter.provisioned == []
    assert adapter.revoked == []
    assert store.config.sources == []


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


# --- Additive provider-presentation metadata (ratified 2026-09-19) -----------
#
# `vendor`, `display_name` and `masked_credential` let a client name and draw the
# provider a credential belongs to. They exist because `backend` cannot: it says
# which CLI held the key, not whose key it is, and an OpenCode row can come from
# either of two stores. The tests below hold the three properties that make them
# safe to add — every source shape carries them, the masked credential is the same
# masking the composed detail already used, and no pre-existing value or id moves.

_KEY_FROM_OPENCODE_CONFIG = "sk-config-deepseek-123456"
_KEY_FROM_OPENCODE_AUTH = "sk-auth-zhipu-123456"
_CLAUDE_KEY = "sk-ant-meta-123456789"
_CLAUDE_TOKEN = "bearer-meta-123456"
_CODEX_KEY = "sk-openai-meta-123456"

_ALL_PLAINTEXT = (
    _KEY_FROM_OPENCODE_CONFIG,
    _KEY_FROM_OPENCODE_AUTH,
    _CLAUDE_KEY,
    _CLAUDE_TOKEN,
    _CODEX_KEY,
    "claude-oauth-meta-token",
    "codex-oauth-meta-token",
)


def _write_key_sources(home: Path) -> None:
    """Every shape that yields an importable key, in one home."""
    _write(
        home / ".claude" / "settings.json",
        json.dumps({"env": {"ANTHROPIC_API_KEY": _CLAUDE_KEY, "ANTHROPIC_AUTH_TOKEN": _CLAUDE_TOKEN}}),
    )
    _write(home / ".codex" / "auth.json", json.dumps({"OPENAI_API_KEY": _CODEX_KEY}))
    _write(home / ".codex" / "config.toml", 'cli_auth_credentials_store = "file"\n')
    # deepseek keeps its key in the OpenCode config file, zhipuai in auth.json —
    # the two stores `backend` + `kind` cannot tell apart.
    _write(
        home / ".config" / "opencode" / "opencode.json",
        json.dumps(
            {
                "provider": {
                    "deepseek": {"options": {"apiKey": _KEY_FROM_OPENCODE_CONFIG}},
                    "zhipuai": {"options": {"baseURL": "https://zhipu.example/v1"}},
                }
            }
        ),
    )
    _write(
        home / ".local" / "share" / "opencode" / "auth.json",
        json.dumps({"zhipuai": {"type": "api", "key": _KEY_FROM_OPENCODE_AUTH}}),
    )
    _write(
        home / ".cache" / "opencode" / "models.json",
        json.dumps(
            {
                "deepseek": {
                    "id": "deepseek",
                    "npm": "@ai-sdk/openai-compatible",
                    "api": "https://api.deepseek.com/v1",
                },
                "zhipuai": {
                    "id": "zhipuai",
                    "npm": "@ai-sdk/openai-compatible",
                    "api": "https://zhipu.example/v1",
                },
            }
        ),
    )


def _write_oauth_sources(home: Path) -> None:
    """The subscription shapes with complete refresh-capable grants."""
    _write(
        home / ".claude" / ".credentials.json",
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "claude-oauth-meta-token",
                    "refreshToken": "claude-refresh-meta-token",
                }
            }
        ),
    )
    _write(
        home / ".codex" / "auth.json",
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "codex-oauth-meta-token",
                    "refresh_token": "codex-refresh-meta-token",
                    "account_id": "acct_codex_meta",
                },
            }
        ),
    )
    _write(home / ".codex" / "config.toml", 'cli_auth_credentials_store = "file"\n')


def test_mh_mig_003_scan_rows_name_their_provider_without_exposing_plaintext(
    tmp_path: Path,
) -> None:
    """MH-MIG-003: every shape the scan produces carries the provider it belongs
    to, and no shape carries a plaintext credential."""
    home = tmp_path / "keys"
    _write_key_sources(home)
    items = scan_native_configs(ModelHubConfig(), home=home, mask_credential=_mask_credential)
    payload = {"items": [item.to_payload() for item in items]}
    _validate_scan(payload)

    by_shape = {
        (row["backend"], row["kind"], row["proposed_action"]): row for row in payload["items"]
    }
    # Claude's API key and its Auth Token, Codex's API key, and an OpenCode key
    # from each of the two stores. The Auth Token is `reauth` and still needs a
    # name to show, so metadata is not conditional on being importable.
    assert set(by_shape) == {
        ("claude", "api_key", "import"),
        ("claude", "api_key", "reauth"),
        ("codex", "api_key", "import"),
        ("opencode", "opencode_provider", "import"),
    }
    assert len([r for r in payload["items"] if r["backend"] == "opencode"]) == 2

    for row in payload["items"]:
        assert row["vendor"], row
        assert row["display_name"], row
        if row["masked_credential"] is None:
            assert row["proposed_action"] == "reauth", row
            assert row["masked_detail"] == "", row
        else:
            assert row["masked_credential"], row

    serialized = json.dumps(payload)
    for secret in _ALL_PLAINTEXT:
        assert secret not in serialized


def test_masked_credential_is_the_same_masking_the_detail_already_showed(
    tmp_path: Path,
) -> None:
    home = tmp_path / "keys"
    _write_key_sources(home)
    rows = [
        item.to_payload()
        for item in scan_native_configs(
            ModelHubConfig(), home=home, mask_credential=_mask_credential
        )
    ]

    for row in rows:
        if row["backend"] == "opencode":
            # The composed detail is provider + masked key; splitting it in the
            # client would be re-parsing a display string, so the field carries
            # the same text the composition used.
            assert row["masked_detail"] == f"{row['vendor']} · {row['masked_credential']}"
        else:
            # Claude and Codex show the masked key alone, so the two agree exactly.
            if row["masked_credential"] is None:
                assert row["masked_detail"] == ""
            else:
                assert row["masked_detail"] == row["masked_credential"]

    for plaintext in (_KEY_FROM_OPENCODE_CONFIG, _KEY_FROM_OPENCODE_AUTH, _CLAUDE_KEY):
        masked = _mask_credential(plaintext)
        assert masked != plaintext
        assert any(row["masked_credential"] == masked for row in rows), masked


def test_subscription_rows_carry_a_name_but_no_credential(tmp_path: Path) -> None:
    home = tmp_path / "oauth"
    _write_oauth_sources(home)
    rows = [
        item.to_payload()
        for item in scan_native_configs(
            ModelHubConfig(), home=home, mask_credential=_mask_credential
        )
    ]
    _validate_scan({"items": rows})

    assert {(row["backend"], row["kind"], row["proposed_action"]) for row in rows} == {
        ("claude", "oauth_native", "import"),
        ("codex", "oauth_native", "import"),
    }
    for row in rows:
        assert row["vendor"] and row["display_name"]
        # There is no key here to mask; a client falls back to `masked_detail`.
        assert row["masked_credential"] is None


def test_presentation_metadata_leaves_every_pre_existing_value_and_id_alone(
    tmp_path: Path,
) -> None:
    home = tmp_path / "keys"
    _write_key_sources(home)

    def scan() -> list:
        return scan_native_configs(
            ModelHubConfig(), home=home, mask_credential=_mask_credential
        )

    established = (
        "id",
        "backend",
        "kind",
        "masked_detail",
        "proposed_action",
        "selected",
        "notes_key",
    )
    first = [item.to_payload() for item in scan()]
    second = [item.to_payload() for item in scan()]

    # Ids are content-derived, so a second scan of untouched files must reproduce
    # them exactly — an id that moved would orphan a selection made before it.
    assert [{k: row[k] for k in established} for row in first] == [
        {k: row[k] for k in established} for row in second
    ]
    # And the established half still reads off the dataclass it always did.
    for item, row in zip(scan(), first):
        assert row["id"] == item.id
        assert row["masked_detail"] == item.masked_detail
        assert row["proposed_action"] == item.proposed_action
        assert row["selected"] == item.selected
        assert row["notes_key"] == item.notes_key
