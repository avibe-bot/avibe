"""Canonical static credentials keep exact, fixture-owned cleanup evidence."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from config.v2_config import ModelHubSourceConfig
from core.handlers.model_hub.migration import scan_native_configs
from core.handlers.model_hub.migration_files import plan_native_cleanup
from core.handlers.model_hub.migration_journal import NativeFileEdit, TakeoverStateError
from core.handlers.model_hub.service import ModelHubError
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import _service, _write
from tests.test_model_hub_migration_transport import KEY as HTTP_KEY
from tests.test_model_hub_migration_transport import _real_http_adapter, _upstream
from tests.test_model_hub_persisted_inventory import home  # noqa: F401 - isolated fixture


KEY = "fixture-static-canonical"
RAW = f" \t{KEY} \t"
SHAPES = (
    *(f"claude-{location}-{kind}" for location in ("user", "backup", "project", "local")
      for kind in ("api", "bearer")),
    "codex-config", "codex-project", "codex-auth",
    "opencode-config", "opencode-project", "opencode-auth",
)
CONFIG_SHAPES = tuple(shape for shape in SHAPES if not shape.endswith("-auth"))


def _seed(home, shape, raw=RAW):
    roots = ()
    if shape.startswith("claude"):
        _, location, kind = shape.split("-")
        root = home
        if location in ("project", "local"):
            root = home / "project"
            roots = (root,)
        filename = {
            "backup": ".avibe-oauth-settings-env-backup.json",
            "local": "settings.local.json",
        }.get(location, "settings.json")
        env = {
            "ANTHROPIC_AUTH_TOKEN" if kind == "bearer" else "ANTHROPIC_API_KEY": raw,
            "KEEP_FIXTURE": "preserved",
        }
        if kind == "bearer":
            env["ANTHROPIC_BASE_URL"] = "https://fixture-relay.example"
        path = root / ".claude" / filename
        _write(path, json.dumps({"env": env, "unrelated": "preserved"}))
    elif shape.startswith("codex"):
        if shape == "codex-auth":
            path = home / ".codex/auth.json"
            _write(path, json.dumps({"OPENAI_API_KEY": raw, "unrelated": "preserved"}))
        else:
            root = home / "project" if shape == "codex-project" else home
            roots = (root,) if root != home else ()
            path = root / ".codex/config.toml"
            _write(path, '[model_providers.fixture]\nname="preserved"\n'
                   f"experimental_bearer_token={json.dumps(raw)}\n")
    elif shape == "opencode-auth":
        path = home / ".local/share/opencode/auth.json"
        _write(path, json.dumps({
            "openai": {"type": "api", "key": raw},
        }))
    else:
        root = home / "project" if shape == "opencode-project" else home / ".config/opencode"
        roots = (root,) if shape == "opencode-project" else ()
        path = root / "opencode.json"
        _write(path, json.dumps({
            "provider": {"openai": {"options": {"apiKey": raw, "timeout": 1234}}},
            "unrelated": "preserved",
        }))
    return path, roots


def _items(service, roots):
    return scan_native_configs(
        service.store.load(), home=service.migration_home,
        project_roots=roots, mask_credential=lambda _: "masked",
    )


def _selected(items):
    [item] = items
    assert item.proposed_action == "import"
    return item


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("raw", [KEY, RAW, f"\u00a0{KEY}\u2003"])
def test_static_proof_and_custody_use_canonical_value(home, tmp_path, shape, raw):
    path, roots = _seed(home, shape, raw)
    service, store, adapter = _service(tmp_path, migration_home=home)
    service.migration_project_roots = lambda: roots
    proof_values = []
    transient = adapter.provision_transient_credential

    async def capture(vendor, secret, base_url, **kwargs):
        proof_values.append(secret)
        return await transient(vendor, secret, base_url, **kwargs)

    adapter.provision_transient_credential = capture
    item = _selected(_items(service, roots))
    assert item.secret == KEY
    assert raw not in json.dumps(item.to_payload()) and KEY not in repr(item)
    result = asyncio.run(service.migration_apply([item.id]))
    assert result["applied"] == 1
    [source] = store.config.sources
    assert proof_values == [adapter.keys[source.credential_ref][2]] == [KEY]
    assert adapter.auth_schemes[source.credential_ref] == item.auth_scheme
    assert KEY.encode() not in path.read_bytes()
    if shape == "opencode-auth":
        assert json.loads(path.read_bytes()) == {}
    else:
        assert b"preserved" in path.read_bytes()
    assert service.migration_journal.load() is None


@pytest.mark.parametrize("shape", CONFIG_SHAPES)
def test_static_cleanup_retains_exact_raw_snapshot_for_reverse(home, tmp_path, shape):
    path, roots = _seed(home, shape)
    before = path.read_bytes()
    service, _, _ = _service(tmp_path, migration_home=home)
    [item] = _items(service, roots)
    edits = plan_native_cleanup([item], home=home, project_roots=roots)
    [owned] = [edit for edit in edits if edit.path == path]
    assert owned.before == before
    assert KEY.encode() not in owned.after
    for edit in edits:
        NativeFileEdit.from_payload(edit.to_payload()).apply()
    for edit in reversed(edits):
        NativeFileEdit.from_payload(edit.to_payload()).apply(reverse=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize("shape", CONFIG_SHAPES)
def test_static_canonical_value_reuses_existing_ref(home, tmp_path, shape):
    path, roots = _seed(home, shape)
    service, store, adapter = _service(tmp_path, migration_home=home)
    service.migration_project_roots = lambda: roots
    [item] = _items(service, roots)
    ref = asyncio.run(adapter.provision_credential(
        item.vendor, item.protocol, KEY, item.base_url, auth_scheme=item.auth_scheme,
    ))
    store.config.sources.append(ModelHubSourceConfig.from_payload({
        "id": "src_staticfixture", "kind": "api_key", "vendor": item.vendor,
        "display_name": "Existing fixture", "protocol": item.protocol,
        "base_url": item.base_url, "supply_channel": "hub", "billing": "metered",
        "state": {"status": "standby"}, "models": [], "credential_ref": ref,
    }))
    asyncio.run(service.migration_apply([item.id]))
    assert len(adapter.provisioned) == len(store.config.sources) == 1
    assert store.config.sources[0].credential_ref == ref
    assert adapter.observed and not adapter.transient_refs
    assert KEY.encode() not in path.read_bytes()


@pytest.mark.parametrize("shape", CONFIG_SHAPES)
def test_other_canonical_credential_is_left_in_place(home, tmp_path, shape):
    path, roots = _seed(home, shape)
    before = path.read_bytes()
    service, _, adapter = _service(tmp_path, migration_home=home)
    [item] = _items(service, roots)
    edits = plan_native_cleanup([replace(item, secret="fixture-other")], home=home, project_roots=roots)
    # An unselected credential stays native; the Hub launch shadows it.
    assert next(edit for edit in edits if edit.path == path).after == before
    assert path.read_bytes() == before and not adapter.provisioned


@pytest.mark.parametrize("shape", CONFIG_SHAPES)
@pytest.mark.parametrize("when", ["after-scan", "during-proof"])
def test_padding_only_change_never_bypasses_exact_consent(home, tmp_path, shape, when):
    path, roots = _seed(home, shape)
    service, store, adapter = _service(tmp_path, migration_home=home)
    service.migration_project_roots = lambda: roots
    [item] = _items(service, roots)
    changed = None

    def change():
        nonlocal changed
        _seed(home, shape, f"  {KEY}  ")
        changed = path.read_bytes()

    if when == "after-scan":
        change()
    else:
        observe = adapter.observe_source

        async def mutate(*args, **kwargs):
            change()
            return await observe(*args, **kwargs)

        adapter.observe_source = mutate
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply([item.id]))
    assert changed is not None and path.read_bytes() == changed
    assert not store.config.sources
    assert not adapter.oauth_provisioned
    assert service.migration_journal.load() is None
    if when == "after-scan":
        assert not adapter.provisioned and not adapter.observed
    else:
        assert {entry[2] for entry in adapter.provisioned} <= set(adapter.revoked)


@pytest.mark.parametrize("value", [" \t ", "fixture-other", ["fixture"], {"key": KEY}, 123])
@pytest.mark.parametrize("field", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"])
def test_unselected_settings_value_stays_while_selected_key_is_cleaned(home, tmp_path, value, field):
    path, _ = _seed(home, "claude-user-api", KEY)
    # A real second native layer, not a new supported filename.
    project = home / "project"
    other = project / ".claude/settings.local.json"
    _write(other, json.dumps({"env": {field: value}}))
    before = other.read_bytes()
    service, _, adapter = _service(tmp_path, migration_home=home)
    selected = next(item for item in _items(service, (project,)) if item.secret == KEY)
    edits = {edit.path: edit for edit in plan_native_cleanup([selected], home=home, project_roots=(project,))}
    assert KEY.encode() not in edits[path].after
    assert edits[other.absolute()].after == before
    assert not adapter.provisioned


def test_padded_settings_bearer_uses_proven_header_after_custody(home, tmp_path):
    async def scenario():
        async with _upstream("bearer") as (origin, requests):
            path = home / ".claude/settings.json"
            _write(path, json.dumps({"env": {
                "ANTHROPIC_AUTH_TOKEN": f" \t{HTTP_KEY} \t",
                "ANTHROPIC_BASE_URL": origin, "KEEP_FIXTURE": "preserved",
            }}))
            service, store, _ = _service(tmp_path, migration_home=home)
            runtime = _real_http_adapter(service, tmp_path)
            [row] = service.migration_scan()["items"]
            await service.migration_apply([row["id"]])
            [source] = store.config.sources
            assert runtime.state_store.read_api_key(source.credential_ref) == HTTP_KEY
            assert await runtime.credential_auth_scheme(source.credential_ref) == "bearer"
            requests.clear()
            assert await runtime.discover_models("anthropic", "anthropic", origin, source.credential_ref)
            assert any(headers.get("authorization") == f"Bearer {HTTP_KEY}" for _, headers in requests)
            assert all("x-api-key" not in headers for _, headers in requests)
            assert json.loads(path.read_bytes()) == {"env": {"KEEP_FIXTURE": "preserved"}}

    asyncio.run(scenario())
