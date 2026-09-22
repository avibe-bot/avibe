"""Persisted migration discovery and consent never depend on runtime secrets."""

import asyncio
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from config.v2_config import ModelHubConfig
from core.handlers.model_hub.migration import scan_native_configs
from core.handlers.model_hub.migration_files import (
    NativeReferenceError,
    native_config_references,
    plan_native_cleanup,
)
from core.handlers.model_hub.migration_journal import NativeFileEdit, TakeoverStateError
from core.handlers.model_hub.service import ModelHubError
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import (
    _isolate_native_home,
    _service,
    _write,
)
from tests.test_native_oauth_store import FakeKeychain
from vibe import native_oauth_store


@pytest.fixture
def home(tmp_path, monkeypatch):
    result = tmp_path / "native"
    result.mkdir()
    _isolate_native_home(monkeypatch, result)
    monkeypatch.setenv("XDG_DATA_HOME", str(result / ".local/share"))
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(result / ".claude"))
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(native_oauth_store, "_KEYCHAIN_STORE", FakeKeychain())

    def forbidden(*args, **kwargs):
        raise AssertionError("real native credential boundary reached")

    for name in ("metadata", "read", "write", "delete"):
        monkeypatch.setattr(native_oauth_store._SecurityKeychainStore, name, forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess.Popen, "__init__", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    return result


def scan(home):
    return scan_native_configs(ModelHubConfig(), home=home, mask_credential=lambda _: "masked")


def config(home, backend, providers):
    if backend == "codex":
        path = home / ".codex/config.toml"
        _write(path, providers)
    else:
        path = home / ".config/opencode/opencode.json"
        _write(path, json.dumps({"provider": providers}))
    return path


def test_runtime_authentication_is_never_read(home, monkeypatch):
    names = (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL",
        "OPENROUTER_API_KEY", "OPENCODE_CONFIG_CONTENT", "CUSTOM_FIXTURE_KEY",
    )
    for name in names:
        monkeypatch.setenv(name, "fixture-inherited")
    original = os.environ.get

    def guarded(name, *args):
        assert name not in names, f"runtime credential read: {name}"
        return original(name, *args)

    monkeypatch.setattr(os.environ, "get", guarded)
    assert scan(None) == []
    path = config(home, "codex", '[model_providers.fixture]\nenv_key = "CUSTOM_FIXTURE_KEY"\n')
    [row] = scan(None)
    assert row.notes_key.endswith(".reference")
    assert row.source_paths == (str(path),)


@pytest.mark.parametrize("backend", ["codex", "opencode"])
def test_custom_reference_uses_only_saved_literal_and_cleanup(home, monkeypatch, backend):
    shell = home / ".zshrc"
    shell.write_bytes(b"# keep \xe4\xb8\xad\xe6\x96\x87\r\nexport CUSTOM_FIXTURE_KEY='fixture-saved'\r\nalias ll='ls -l'\r\n")
    monkeypatch.setenv("CUSTOM_FIXTURE_KEY", "fixture-inherited")
    if backend == "codex":
        path = config(home, backend, '[model_providers.fixture]\nenv_key = "CUSTOM_FIXTURE_KEY"\n')
    else:
        path = config(home, backend, {"openai": {"options": {"apiKey": "{env:CUSTOM_FIXTURE_KEY}"}}})
    [row] = scan(home)
    assert row.secret == "fixture-saved"
    assert set(row.source_paths) == {str(path), str(shell)}
    assert row.to_payload()["source_paths"] == list(row.source_paths)
    assert "fixture-saved" not in json.dumps(row.to_payload())
    assert "fixture-saved" not in repr(row)
    before = shell.read_bytes()
    edits = plan_native_cleanup([row], home=home)
    for edit in edits:
        edit.apply()
    assert shell.read_bytes() == b"# keep \xe4\xb8\xad\xe6\x96\x87\r\nalias ll='ls -l'\r\n"
    assert os.environ["CUSTOM_FIXTURE_KEY"] == "fixture-inherited"
    for edit in reversed(edits):
        NativeFileEdit.from_payload(edit.to_payload()).apply(reverse=True)
    assert shell.read_bytes() == before


@pytest.mark.parametrize("contents,reason", [
    ("export CUSTOM_FIXTURE_KEY=$(echo forbidden)\n", "dynamic_shell"),
    ("export CUSTOM_FIXTURE_KEY=one\nexport CUSTOM_FIXTURE_KEY=two\n", "ambiguous_shell"),
    ("", "reference"),
])
def test_persisted_reference_refusals_are_specific(home, contents, reason):
    shell = home / ".profile"
    shell.write_text(contents)
    path = config(home, "opencode", {"openai": {"options": {"apiKey": "{env:CUSTOM_FIXTURE_KEY}"}}})
    [row] = scan(home)
    assert row.notes_key.endswith(f".{reason}")
    assert str(path) in row.source_paths
    if contents:
        assert str(shell) in row.source_paths
    assert not row.selected
    assert shell.read_text() == contents


def test_auth_file_fallback_ignores_runtime_reference(home, monkeypatch):
    monkeypatch.setenv("CUSTOM_FIXTURE_KEY", "runtime")
    config(home, "opencode", {"openai": {"options": {"apiKey": "{env:CUSTOM_FIXTURE_KEY}"}}})
    auth = home / ".local/share/opencode/auth.json"
    _write(auth, json.dumps({"openai": {"type": "api", "key": "fixture-auth"}}))
    [row] = scan(home)
    assert row.secret == "fixture-auth"
    assert str(auth) in row.source_paths
    for edit in plan_native_cleanup([row], home=home):
        edit.apply()
    assert scan(home) == []


def test_conflicting_shell_profiles_never_guess_key_target(home):
    (home / ".profile").write_text("export OPENAI_API_KEY=fixture-one\nexport OPENAI_BASE_URL=https://one.example/v1\n")
    (home / ".zshrc").write_text("export OPENAI_API_KEY=fixture-two\nexport OPENAI_BASE_URL=https://two.example/v1\n")
    [row] = scan(home)
    assert row.notes_key.endswith(".ambiguous_shell")
    assert set(row.source_paths) == {str(home / ".profile"), str(home / ".zshrc")}


def test_standalone_literals_preserve_unselected_backend_and_journal_guards(home):
    shell = home / ".profile"
    shell.write_text(
        "# keep\nexport ANTHROPIC_API_KEY=fixture-claude\n"
        "export ANTHROPIC_BASE_URL=https://anthropic.example\n"
        "export OPENAI_API_KEY=fixture-codex\n"
    )
    rows = scan(home)
    selected = [row for row in rows if row.backend == "claude"]
    edits = plan_native_cleanup(selected, home=home)
    absent = next(edit for edit in edits if edit.path == home / ".bashrc")
    assert absent.before is None and absent.after is None
    before = shell.read_bytes()
    for edit in edits:
        NativeFileEdit.from_payload(edit.to_payload()).apply()
    assert shell.read_text() == "# keep\nexport OPENAI_API_KEY=fixture-codex\n"
    for edit in reversed(edits):
        edit.apply(reverse=True)
    assert shell.read_bytes() == before
    (home / ".bashrc").write_text("export OPENAI_API_KEY=concurrent\n")
    with pytest.raises(TakeoverStateError):
        absent.check()


def shared(home):
    shell = home / ".zshrc"
    shell.write_text("export OPENAI_API_KEY=fixture-shared\n")
    path = config(home, "opencode", {"openai": {"options": {"apiKey": "{env:OPENAI_API_KEY}"}}})
    return shell, path


def test_shared_consent_is_public_and_partial_selection_stops_before_provision(home, tmp_path):
    shell, path = shared(home)
    service, store, adapter = _service(tmp_path, migration_home=home)
    rows = service.migration_scan()["items"]
    assert {row["backend"] for row in rows} == {"codex", "opencode"}
    assert all(row["required_backends"] == ["codex", "opencode"] for row in rows)
    before = shell.read_bytes(), path.read_bytes()
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply([rows[0]["id"]]))
    assert not adapter.provisioned and not adapter.transient_refs
    assert (shell.read_bytes(), path.read_bytes()) == before
    assert asyncio.run(service.migration_apply([row["id"] for row in rows]))["applied"] == 2
    assert len(adapter.provisioned) == 1
    assert len(store.config.sources) == 1
    assert shell.read_bytes() == b""


def test_new_shared_consumer_during_provision_preserves_original_sources(home, tmp_path):
    shell = home / ".profile"
    shell.write_text("export OPENAI_API_KEY=fixture-selected\n")
    service, store, adapter = _service(tmp_path, migration_home=home)
    [row] = service.migration_scan()["items"]
    provision = adapter.provision_credential
    before = shell.read_bytes()

    async def race(*args, **kwargs):
        ref = await provision(*args, **kwargs)
        config(home, "opencode", {"openai": {"options": {"apiKey": "{env:OPENAI_API_KEY}"}}})
        return ref

    adapter.provision_credential = race
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply([row["id"]]))
    assert shell.read_bytes() == before
    assert not store.config.sources
    assert len(adapter.revoked) == len(adapter.provisioned) == 1


def test_consumer_added_after_scan_invalidates_old_consent(home, tmp_path):
    (home / ".profile").write_text("export OPENAI_API_KEY=fixture-selected\n")
    service, _, adapter = _service(tmp_path, migration_home=home)
    [row] = service.migration_scan()["items"]
    config(home, "opencode", {"openai": {"options": {"apiKey": "{env:OPENAI_API_KEY}"}}})
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply([row["id"]]))
    assert not adapter.provisioned


@pytest.mark.parametrize("name,reason", [
    ("apiKeyHelper", "helper"), ("CLAUDE_CODE_OAUTH_TOKEN", "token"),
])
def test_claude_unsupported_credential_names_remain_specific(home, name, reason):
    path = home / ".claude/settings.json"
    payload = {name: "fixture"} if name == "apiKeyHelper" else {"env": {name: "fixture"}}
    _write(path, json.dumps(payload))
    [row] = scan(home)
    assert row.notes_key.endswith(f".{reason}")
    assert row.source_paths == (str(path),)


@pytest.mark.parametrize("location", ["settings", "shell"])
@pytest.mark.parametrize("target,secret,allowed", [
    ("https://relay.example/anthropic", "fixture-bearer", True),
    ("https://api.anthropic.com", "fixture-bearer", False),
    ("https://relay.example/anthropic", "sk-ant-oat-fixture", False),
])
def test_anthropic_bearer_semantics_are_explicit_and_private(home, location, target, secret, allowed):
    if location == "shell":
        path = home / ".profile"
        path.write_text(f"export ANTHROPIC_AUTH_TOKEN={secret}\nexport ANTHROPIC_BASE_URL={target}\n")
    else:
        path = home / ".claude/settings.json"
        _write(path, json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": secret, "ANTHROPIC_BASE_URL": target}}))
    [row] = scan(home)
    assert row.selected is allowed
    assert row.source_paths == (str(path),)
    if allowed:
        assert row.auth_scheme == "bearer"
        assert row.secret == secret
    else:
        assert row.notes_key.endswith(".token")
    assert secret not in json.dumps(row.to_payload())
    assert secret not in repr(row)
    assert "auth_scheme" not in row.to_payload()


def test_claude_key_and_bearer_are_both_retained(home):
    path = home / ".claude/settings.json"
    _write(path, json.dumps({"env": {
        "ANTHROPIC_API_KEY": "fixture-api-key", "ANTHROPIC_AUTH_TOKEN": "fixture-bearer",
        "ANTHROPIC_BASE_URL": "https://relay.example/anthropic",
    }}))
    rows = scan(home)
    assert {(row.secret, row.auth_scheme) for row in rows} == {
        ("fixture-api-key", None), ("fixture-bearer", "bearer"),
    }
    assert len({row.id for row in rows}) == 2


def test_bearer_scheme_reaches_proof_and_custody_and_prevents_wrong_reuse(home, tmp_path):
    path = home / ".claude/settings.json"
    _write(path, json.dumps({"env": {
        "ANTHROPIC_AUTH_TOKEN": "fixture-same-bytes",
        "ANTHROPIC_BASE_URL": "https://relay.example/anthropic",
    }}))
    service, store, adapter = _service(tmp_path, migration_home=home)
    provision = adapter.provision_credential
    transient = adapter.provision_transient_credential
    matches = adapter.matches_api_key_credential
    schemes = {}
    proof_schemes = []

    async def provision_with_scheme(*args, **kwargs):
        scheme = kwargs.pop("auth_scheme", None)
        ref = await provision(*args, **kwargs)
        schemes[ref] = scheme
        return ref

    async def transient_with_scheme(*args, **kwargs):
        proof_schemes.append(kwargs.pop("auth_scheme", None))
        return await transient(*args, **kwargs)

    async def matches_with_scheme(ref, *args, auth_scheme=None):
        return schemes.get(ref) == auth_scheme and await matches(ref, *args)

    adapter.provision_credential = provision_with_scheme
    adapter.provision_transient_credential = transient_with_scheme
    adapter.matches_api_key_credential = matches_with_scheme
    # A pre-existing legacy Source has the same bytes/target but a different
    # header scheme. It must not be reused for the new saved Bearer token.
    from config.v2_config import ModelHubSourceConfig
    legacy_ref = asyncio.run(adapter.provision_credential(
        "anthropic", "anthropic", "fixture-same-bytes", "https://relay.example/anthropic",
    ))
    store.config.sources.append(ModelHubSourceConfig.from_payload({
        "id": "src_legacyfixture", "kind": "api_key", "vendor": "anthropic",
        "display_name": "Legacy fixture", "protocol": "anthropic",
        "base_url": "https://relay.example/anthropic",
        "supply_channel": "hub", "billing": "metered",
        "state": {"status": "standby"}, "models": [], "credential_ref": legacy_ref,
    }))
    rows = service.migration_scan()["items"]
    ids = [row["id"] for row in rows]
    assert asyncio.run(service.migration_apply(ids))["applied"] == 1
    assert proof_schemes == ["bearer"]
    assert set(schemes.values()) == {None, "bearer"}
    assert len(store.config.sources) == len(adapter.provisioned) == 2
    assert "auth_scheme" not in json.dumps(store.config.to_payload())
    assert asyncio.run(service.migration_apply(ids))["applied"] == 1
    assert len(adapter.provisioned) == 2


def test_deduplication_unions_all_source_paths(home):
    shell = home / ".profile"
    shell.write_text("export CUSTOM_FIXTURE_KEY=fixture-one\n")
    first = config(home, "opencode", {"openai": {"options": {"apiKey": "{env:CUSTOM_FIXTURE_KEY}"}}})
    second = first.with_suffix(".jsonc")
    _write(second, first.read_text())
    [row] = scan(home)
    assert set(row.source_paths) == {str(first), str(second), str(shell)}


def test_empty_literal_does_not_create_a_credential(home):
    (home / ".profile").write_text("export OPENAI_API_KEY=''\n")
    assert scan(home) == []


def test_empty_assignment_conflicts_with_a_different_profile_value(home):
    (home / ".profile").write_text("export OPENAI_API_KEY=''\n")
    (home / ".zshrc").write_text("export OPENAI_API_KEY=fixture-key\n")
    [row] = scan(home)
    assert row.notes_key.endswith(".ambiguous_shell")


def test_blocked_header_consumer_is_part_of_shared_consent(home):
    (home / ".profile").write_text("export OPENAI_API_KEY=fixture-shared\n")
    config(home, "opencode", {"openai": {"options": {
        "headers": {"Authorization": "Bearer {env:OPENAI_API_KEY}"},
    }}})
    rows = scan(home)
    assert {row.backend for row in rows} == {"codex", "opencode"}
    assert all(row.required_backends == ("codex", "opencode") for row in rows)
    [blocked] = [row for row in rows if row.backend == "opencode"]
    assert blocked.notes_key.endswith(".headers")


def test_malformed_unselected_config_cannot_hide_a_shell_consumer(home):
    (home / ".profile").write_text("export OPENAI_API_KEY=fixture-shared\n")
    path = home / ".config/opencode/opencode.json"
    _write(path, '{"provider":')
    rows = scan(home)
    [codex] = [row for row in rows if row.backend == "codex"]
    assert codex.notes_key.endswith(".config")
    assert not codex.selected
    assert str(path) in codex.source_paths


def test_absent_profile_created_during_native_key_proof_blocks_cleanup(home, tmp_path):
    path = home / ".claude/settings.json"
    _write(path, json.dumps({"env": {"ANTHROPIC_API_KEY": "fixture-selected"}}))
    service, store, adapter = _service(tmp_path, migration_home=home)
    [row] = service.migration_scan()["items"]
    provision = adapter.provision_credential
    before = path.read_bytes()

    async def race(*args, **kwargs):
        ref = await provision(*args, **kwargs)
        (home / ".profile").write_text("export ANTHROPIC_API_KEY=fixture-concurrent\n")
        return ref

    adapter.provision_credential = race
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply([row["id"]]))
    assert path.read_bytes() == before
    assert not store.config.sources
    assert len(adapter.revoked) == len(adapter.provisioned) == 1


@pytest.mark.parametrize("selected_opencode_vendor", [False, True])
def test_live_base_reference_survives_actual_planned_cleanup(home, tmp_path, selected_opencode_vendor):
    shell = home / ".profile"
    key_line = b"export OPENAI_API_KEY=fixture-codex\r\n"
    base_line = b"export OPENAI_BASE_URL=https://fixture.example/v1\r\n"
    shell.write_bytes(key_line + base_line)
    providers = {"openai": {"options": {"baseURL": "{env:OPENAI_BASE_URL}"}}}
    if selected_opencode_vendor:
        providers["zhipuai"] = {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"apiKey": "fixture-zhipu", "baseURL": "https://fixture-zhipu.example/v1"},
        }
    path = config(home, "opencode", providers)
    original = path.read_bytes()
    service, _, adapter = _service(tmp_path, migration_home=home)
    rows = service.migration_scan()["items"]
    assert len(rows) == 1 + selected_opencode_vendor
    assert all(row["proposed_action"] == "import" for row in rows)
    assert all("shell_auth_variables" not in row for row in rows)
    assert asyncio.run(service.migration_apply([row["id"] for row in rows]))["applied"] == len(rows)
    assert shell.read_bytes() == base_line
    after = json.loads(path.read_bytes())
    assert after["provider"]["openai"] == providers["openai"]
    if selected_opencode_vendor:
        assert "options" not in after["provider"]["zhipuai"]
    else:
        assert path.read_bytes() == original
    assert len(adapter.provisioned) == len(rows)


@pytest.mark.parametrize("root_reference", [False, True])
def test_live_auth_reference_without_a_row_blocks_before_any_write(home, tmp_path, root_reference):
    shell = home / ".profile"
    shell.write_text("export OPENAI_API_KEY=fixture-codex\n")
    if root_reference:
        path = config(home, "opencode", {})
        _write(path, json.dumps({"instructions": ["{env:OPENAI_API_KEY}"]}))
    else:
        path = config(home, "opencode", {"openai": {"options": {"baseURL": "{env:OPENAI_API_KEY}"}}})
    original = shell.read_bytes(), path.read_bytes()
    service, store, adapter = _service(tmp_path, migration_home=home)
    [row] = service.migration_scan()["items"]
    assert row["backend"] == "codex"
    assert row["notes_key"].endswith(".reference")
    assert str(path) in row["source_paths"]
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply([row["id"]]))
    assert (shell.read_bytes(), path.read_bytes()) == original
    assert not adapter.provisioned and not adapter.transient_refs
    assert not store.config.sources


@pytest.mark.parametrize("same_name_for_base", [False, True])
def test_same_backend_surviving_reference_uses_auth_role_over_base_role(home, same_name_for_base):
    shell = home / ".profile"
    shell.write_text("export CUSTOM_FIXTURE_KEY=https://fixture.example/v1\n")
    selected_options = {"apiKey": "{env:CUSTOM_FIXTURE_KEY}"}
    if same_name_for_base:
        selected_options["baseURL"] = "{env:CUSTOM_FIXTURE_KEY}"
    path = config(home, "opencode", {
        "openai": {"options": selected_options},
        "orphan": {"options": {"baseURL": "{env:CUSTOM_FIXTURE_KEY}"}},
    })
    [row] = scan(home)
    assert row.shell_auth_variables == ("CUSTOM_FIXTURE_KEY",)
    assert row.notes_key.endswith(".reference")
    original = shell.read_bytes(), path.read_bytes()
    # Planner enforcement is independent of scan presentation and backend
    # selection: selecting OpenCode does not remove the orphan provider.
    with pytest.raises(NativeReferenceError) as error:
        plan_native_cleanup([replace(row, proposed_action="import", selected=True)], home=home)
    assert error.value.references == {"CUSTOM_FIXTURE_KEY": (str(path),)}
    assert (shell.read_bytes(), path.read_bytes()) == original


def test_only_base_variable_with_live_reference_is_retained_for_same_backend(home):
    shell = home / ".profile"
    key_line = "export CUSTOM_FIXTURE_KEY=fixture-key\n"
    base_line = "export CUSTOM_FIXTURE_BASE=https://fixture.example/v1\n"
    shell.write_text(key_line + base_line)
    path = config(home, "opencode", {
        "openai": {"options": {
            "apiKey": "{env:CUSTOM_FIXTURE_KEY}", "baseURL": "{env:CUSTOM_FIXTURE_BASE}",
        }},
        "orphan": {"options": {"baseURL": "{env:CUSTOM_FIXTURE_BASE}"}},
    })
    [row] = scan(home)
    assert row.proposed_action == "import"
    assert row.shell_auth_variables == ("CUSTOM_FIXTURE_KEY",)
    edits = plan_native_cleanup([row], home=home)
    config_edit = next(edit for edit in edits if edit.path == path)
    references = native_config_references("opencode", json.loads(config_edit.after))
    assert references == {"CUSTOM_FIXTURE_BASE"}
    for edit in edits:
        edit.apply()
    assert shell.read_text() == base_line
    assert "CUSTOM_FIXTURE_KEY" not in references


def test_cross_project_reference_with_no_row_is_also_preserved(home, tmp_path):
    shell = home / ".profile"
    shell.write_text(
        "export OPENAI_API_KEY=fixture-key\n"
        "export OPENAI_BASE_URL=https://fixture.example/v1\n"
    )
    project = tmp_path / "project"
    path = project / "opencode.jsonc"
    _write(path, '{"provider":{"openai":{"options":{"baseURL":"{env:OPENAI_BASE_URL}"}}}}')
    original = path.read_bytes()
    rows = scan_native_configs(
        ModelHubConfig(), home=home, project_roots=(project,), mask_credential=lambda _: "masked",
    )
    assert len(rows) == 1
    for edit in plan_native_cleanup(rows, home=home, project_roots=(project,)):
        edit.apply()
    assert shell.read_text() == "export OPENAI_BASE_URL=https://fixture.example/v1\n"
    assert path.read_bytes() == original
