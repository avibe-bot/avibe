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


def test_codex_retained_bearer_provider_keeps_its_selectors(home, tmp_path):
    path = home / ".codex/config.toml"
    _write(path, 'model_provider = "relay"\n'
           '[profiles.work]\nmodel_provider = "relay"\n'
           '[model_providers.relay]\nbase_url = "https://relay.example/v1"\n'
           'experimental_bearer_token = "fixture-unselected-bearer"\n')
    service, _, _ = _service(tmp_path, migration_home=home)
    [item] = _items(service, ())
    # Another credential (e.g. OAuth) is imported while `relay` is active.
    other = replace(item, kind="oauth_native", secret=None, native_provider_id="relay")
    edits = plan_native_cleanup([other], home=home)
    for edit in edits:
        edit.apply()
    content = path.read_text()
    assert content.count('model_provider = "relay"') == 2
    assert "fixture-unselected-bearer" in content and "relay.example" in content


def test_opencode_shell_key_leaves_a_same_vendor_oauth_entry_native(home, tmp_path):
    auth = home / ".local/share/opencode/auth.json"
    oauth = {"type": "oauth", "access": "fixture-access", "refresh": "fixture-refresh", "expires": 1}
    _write(auth, json.dumps({"openrouter": oauth}))
    _write(home / ".profile", f"export OPENROUTER_API_KEY='{KEY}'\n")
    service, _, adapter = _service(tmp_path, migration_home=home)
    rows = [row for row in service.migration_scan()["items"] if row["proposed_action"] == "import"]
    assert len(rows) == 1
    assert asyncio.run(service.migration_apply([rows[0]["id"]]))["applied"] == 1
    assert adapter.provisioned
    assert json.loads(auth.read_text())["openrouter"] == oauth


@pytest.mark.parametrize("field", ["http_headers", "env_http_headers"])
def test_codex_retained_header_provider_keeps_its_configuration(home, tmp_path, field):
    path = home / ".codex/config.toml"
    header = '{ Authorization = "Bearer fixture-header" }' if field == "http_headers" else '{ Authorization = "RELAY_TOKEN" }'
    _write(path, 'model_provider = "relay"\n'
           '[model_providers.relay]\nbase_url = "https://relay.example/v1"\n'
           f'{field} = {header}\n')
    service, _, _ = _service(tmp_path, migration_home=home)
    [item] = _items(service, ())
    assert item.proposed_action != "import"
    other = replace(item, kind="oauth_native", proposed_action="import", native_provider_id="relay")
    for edit in plan_native_cleanup([other], home=home):
        edit.apply()
    content = path.read_text()
    assert 'model_provider = "relay"' in content
    assert "relay.example" in content and "Authorization" in content


def test_opencode_shell_key_keeps_the_endpoint_a_retained_oauth_entry_uses(home, tmp_path):
    config = home / ".config/opencode/opencode.json"
    auth = home / ".local/share/opencode/auth.json"
    # The OAuth entry's endpoint; the shell key is the only importable row.
    _write(config, json.dumps({"provider": {"openrouter": {"options": {
        "baseURL": "https://relay.example/api/v1"}}}}))
    oauth = {"type": "oauth", "access": "fixture-access", "refresh": "fixture-refresh", "expires": 1}
    _write(auth, json.dumps({"openrouter": oauth}))
    _write(home / ".profile", f"export OPENROUTER_API_KEY='{KEY}'\\n")
    service, _, _ = _service(tmp_path, migration_home=home)
    items = _items(service, ())
    importable = [item for item in items if item.proposed_action == "import"]
    assert importable
    for edit in plan_native_cleanup(importable, home=home, _include_shell=False):
        edit.apply()
    assert json.loads(auth.read_text())["openrouter"] == oauth
    options = json.loads(config.read_text())["provider"]["openrouter"]["options"]
    assert options.get("baseURL") == "https://relay.example/api/v1"


def test_opencode_shell_key_keeps_the_endpoint_a_retained_header_layer_uses(home, tmp_path):
    config = home / ".config/opencode/opencode.json"
    # Header auth is left native; the shell key is the importable row.
    layer = {"baseURL": "https://relay.example/api/v1", "headers": {"Authorization": "Bearer fixture"}}
    _write(config, json.dumps({"provider": {"openrouter": {"options": layer}}}))
    _write(home / ".profile", f"export OPENROUTER_API_KEY='{KEY}'\\n")
    service, _, _ = _service(tmp_path, migration_home=home)
    importable = [item for item in _items(service, ()) if item.proposed_action == "import"]
    assert importable
    for edit in plan_native_cleanup(importable, home=home, _include_shell=False):
        edit.apply()
    assert json.loads(config.read_text())["provider"]["openrouter"]["options"] == layer


@pytest.mark.parametrize("entry", [
    '"bad"',
    '{ http_headers = "bad" }',
    '{ env_http_headers = ["X_KEY"] }',
    '{ base_url = 1 }',
    '{ stream_max_retries = "3" }',
    '{ wire_api = "bogus" }',
    '{ supports_websockets = "bad" }',
    '{ websocket_connect_timeout_ms = -1 }',
    '{ auth = "bad" }',
    '{ aws = { profile = 1 }, http_headers = { X = "y" } }',
    '{ aws = { credential_export = { args = ["x"] } } }',
    '{ auth = { command = "fixture", timeout_ms = 0 } }',
    '{ auth = { command = "fixture", unknown = 1 } }',
    '{ gateway_oauth = { authorization_url = "a", client_id = "b", token_url = "c", delivery = { kind = "query", name = "x" } } }',
])
def test_codex_malformed_provider_entry_blocks_hub_mode(home, tmp_path, entry):
    _write(home / ".codex/config.toml", f"model_providers = {{ relay = {entry} }}\n")
    service, _, _ = _service(tmp_path, migration_home=home)
    assert any(item.backend == "codex" and item.config_blocker for item in _items(service, ()))


@pytest.mark.parametrize("entry", [
    '"bad"', '{"options": "bad"}', '{"options": {"baseURL": 1}}', '{"models": {"m": "bad"}}',
])
def test_opencode_malformed_provider_entry_blocks_hub_mode(home, tmp_path, entry):
    _write(home / ".config/opencode/opencode.json", f'{{"provider": {{"relay": {entry}}}}}')
    service, _, _ = _service(tmp_path, migration_home=home)
    assert any(item.backend == "opencode" and item.config_blocker for item in _items(service, ()))


def test_well_typed_header_providers_do_not_block_hub_mode(home, tmp_path):
    _write(home / ".codex/config.toml", 'model_providers = { relay = { http_headers = { X = "y" }, '
           'supports_websockets = true, wire_api = "responses", future_field = 1, '
           'auth = { command = "fixture", args = ["x"], refresh_interval_ms = 0 }, '
           'aws = { profile = "p", credential_export = { command = "fixture" } }, '
           'gateway_oauth = { authorization_url = "a", client_id = "b", token_url = "c", '
           'delivery = { kind = "header", name = "X" } } } }\n')
    _write(home / ".config/opencode/opencode.json", '{"provider": {"relay": {"options": {"headers": {"X": "y"}}}}}')
    service, _, _ = _service(tmp_path, migration_home=home)
    assert not any(item.config_blocker for item in _items(service, ()))


def test_codex_count_beyond_signed_64_bits_blocks_hub_mode(home, tmp_path):
    _write(home / ".codex/config.toml", "model_providers = { relay = { stream_max_retries = 9223372036854775808 } }\n")
    service, _, _ = _service(tmp_path, migration_home=home)
    assert any(item.backend == "codex" and item.config_blocker for item in _items(service, ()))


def test_apply_rejects_a_backend_whose_native_config_blocks_hub_mode(home, tmp_path):
    _seed(home, "codex-auth")
    _write(home / ".codex/config.toml", 'model_providers = { relay = "bad" }\n')
    service, store, adapter = _service(tmp_path, migration_home=home)
    importable = [row for row in service.migration_scan()["items"] if row["proposed_action"] == "import"]
    assert len(importable) == 1
    with pytest.raises(ModelHubError) as caught:
        asyncio.run(service.migration_apply([importable[0]["id"]]))
    assert caught.value.status == 409
    assert not store.config.sources and not adapter.provisioned
    assert KEY.encode() in (home / ".codex/auth.json").read_bytes()


def test_opencode_header_auth_in_one_layer_keeps_another_layers_endpoint(home, tmp_path):
    user = home / ".config/opencode/opencode.json"
    project = home / "project/opencode.json"
    _write(user, json.dumps({"provider": {"openrouter": {"options": {
        "baseURL": "https://relay.example/api/v1", "apiKey": KEY}}}}))
    _write(project, json.dumps({"provider": {"openrouter": {"options": {
        "headers": {"Authorization": "Bearer fixture"}}}}}))
    roots = (home / "project",)
    service, _, _ = _service(tmp_path, migration_home=home)
    importable = [item for item in _items(service, roots) if item.proposed_action == "import"]
    assert importable
    for edit in plan_native_cleanup(importable, home=home, project_roots=roots, _include_shell=False):
        edit.apply()
    options = json.loads(user.read_text())["provider"]["openrouter"]["options"]
    assert options == {"baseURL": "https://relay.example/api/v1"}


def test_equal_secret_selected_for_another_backend_leaves_claude_oauth_token(home, tmp_path):
    settings = home / ".claude/settings.json"
    _write(settings, json.dumps({"env": {"CLAUDE_CODE_OAUTH_TOKEN": KEY}}))
    _seed(home, "codex-auth", raw=KEY)
    service, _, _ = _service(tmp_path, migration_home=home)
    codex = [item for item in _items(service, ()) if item.backend == "codex" and item.proposed_action == "import"]
    assert codex
    claude = replace(codex[0], backend="claude", secret=None, kind="oauth_native", proposed_action="import")
    for edit in plan_native_cleanup([*codex, claude], home=home):
        edit.apply()
    assert json.loads(settings.read_text())["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == KEY


def test_claude_retained_credential_in_one_layer_keeps_another_layers_base_url(home, tmp_path):
    user = home / ".claude/settings.json"
    project = home / "project/.claude/settings.json"
    _write(user, json.dumps({"env": {"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_BASE_URL": "https://relay.example"}}))
    _write(project, json.dumps({"apiKeyHelper": "fixture-helper"}))
    roots = (home / "project",)
    service, _, _ = _service(tmp_path, migration_home=home)
    importable = [item for item in _items(service, roots) if item.proposed_action == "import"]
    assert importable
    for edit in plan_native_cleanup(importable, home=home, project_roots=roots, _include_shell=False):
        edit.apply()
    assert json.loads(user.read_text())["env"] == {"ANTHROPIC_BASE_URL": "https://relay.example"}


def test_scan_payload_exposes_configuration_blockers(home, tmp_path):
    _write(home / ".codex/config.toml", 'model_providers = { relay = "bad" }\n')
    service, _, _ = _service(tmp_path, migration_home=home)
    rows = service.migration_scan()["items"]
    assert any(row["backend"] == "codex" and row["config_blocker"] for row in rows)


def test_opencode_unselected_key_in_one_layer_keeps_another_layers_endpoint(home, tmp_path):
    user = home / ".config/opencode/opencode.json"
    project = home / "project/opencode.json"
    _write(user, json.dumps({"provider": {"openrouter": {"options": {"apiKey": "fixture-kept-native"}}}}))
    _write(project, json.dumps({"provider": {"openrouter": {"options": {
        "baseURL": "https://relay.example/api/v1", "apiKey": KEY}}}}))
    roots = (home / "project",)
    service, _, _ = _service(tmp_path, migration_home=home)
    importable = [
        item for item in _items(service, roots)
        if item.proposed_action == "import" and item.secret == KEY
    ]
    assert importable
    for edit in plan_native_cleanup(importable, home=home, project_roots=roots, _include_shell=False):
        edit.apply()
    assert json.loads(user.read_text())["provider"]["openrouter"]["options"] == {"apiKey": "fixture-kept-native"}
    assert json.loads(project.read_text())["provider"]["openrouter"]["options"] == {
        "baseURL": "https://relay.example/api/v1"}


def test_codex_unresolved_env_key_in_one_layer_keeps_the_provider_native(home, tmp_path):
    user = home / ".codex/config.toml"
    project = home / "project/.codex/config.toml"
    _write(user, 'model_provider = "relay"\n[model_providers.relay]\nenv_key = "FIXTURE_UNSET_KEY"\n')
    _write(project, '[model_providers.relay]\nbase_url = "https://relay.example/v1"\n'
           f"experimental_bearer_token = {json.dumps(KEY)}\n")
    roots = (home / "project",)
    service, _, _ = _service(tmp_path, migration_home=home)
    items = _items(service, roots)
    importable = [item for item in items if item.proposed_action == "import"]
    assert importable and any(item.proposed_action != "import" for item in items if item.backend == "codex")
    for edit in plan_native_cleanup(importable, home=home, project_roots=roots, _include_shell=False):
        edit.apply()
    assert 'env_key = "FIXTURE_UNSET_KEY"' in user.read_text()
    assert 'model_provider = "relay"' in user.read_text()
    assert "relay.example" in project.read_text()


def test_unresolved_opencode_reference_in_another_layer_stays_with_its_endpoint(home, tmp_path):
    user = home / ".config/opencode/opencode.json"
    project = home / "project/opencode.json"
    _write(user, json.dumps({"provider": {"openrouter": {"options": {"apiKey": "{env:FIXTURE_MISSING_KEY}"}}}}))
    _write(project, json.dumps({"provider": {"openrouter": {"options": {
        "baseURL": "https://relay.example/api/v1", "apiKey": KEY}}}}))
    roots = (home / "project",)
    service, _, _ = _service(tmp_path, migration_home=home)
    importable = [item for item in _items(service, roots) if item.proposed_action == "import"]
    assert importable
    for edit in plan_native_cleanup(importable, home=home, project_roots=roots, _include_shell=False):
        edit.apply()
    assert json.loads(user.read_text())["provider"]["openrouter"]["options"] == {"apiKey": "{env:FIXTURE_MISSING_KEY}"}
    assert json.loads(project.read_text())["provider"]["openrouter"]["options"] == {
        "baseURL": "https://relay.example/api/v1"}
