"""Review regressions consume only synthetic profiles and native stores."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from config.v2_config import ModelHubSourceConfig
from core.handlers.model_hub import migration_journal
from core.handlers.model_hub.migration_journal import NativeFileEdit, TakeoverStateError
from core.handlers.model_hub.migration_shell import cleanup_shell_profile, read_shell_profiles
from core.handlers.model_hub.service import ModelHubError
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import _service, _write
from tests.test_model_hub_migration_transport import KEY as HTTP_KEY
from tests.test_model_hub_migration_transport import _real_http_adapter, _upstream
from tests.test_model_hub_persisted_inventory import home  # noqa: F401 - isolated fixture


KEY = "fixture-canonical-key"
RAW_KEY = f" \t{KEY} \t"


def _seed(home: Path, shape: str) -> tuple[str, Path]:
    backend = shape.split("-")[0]
    name = {
        "claude-api": "ANTHROPIC_API_KEY",
        "claude-bearer": "ANTHROPIC_AUTH_TOKEN",
        "codex-api": "OPENAI_API_KEY",
        "codex-alias": "CODEX_API_KEY",
        "opencode-api": "OPENROUTER_API_KEY",
    }.get(shape, "FIXTURE_SAVED_KEY")
    profile = home / ".profile"
    _write(profile, f"# 保留偏好\r\nexport {name}='{RAW_KEY}'\r\n")
    if shape == "claude-bearer":
        with profile.open("a") as handle:
            handle.write("export ANTHROPIC_BASE_URL='https://fixture.example'\n")
    elif shape == "codex-reference":
        _write(home / ".codex/config.toml", '[model_providers.fixture]\nenv_key="FIXTURE_SAVED_KEY"\n')
    elif shape == "opencode-reference":
        _write(home / ".config/opencode/opencode.json", json.dumps({
            "provider": {"openai": {"options": {"apiKey": "{env:FIXTURE_SAVED_KEY}"}}},
        }))
    return backend, profile


@pytest.mark.parametrize("shape", [
    "claude-api", "claude-bearer", "codex-api", "codex-alias",
    "opencode-api", "codex-reference", "opencode-reference",
])
def test_persisted_key_proof_and_custody_use_one_value(home, tmp_path, shape):
    backend, profile = _seed(home, shape)
    service, store, adapter = _service(tmp_path, migration_home=home)
    proof_values = []
    transient = adapter.provision_transient_credential

    async def capture_proof(vendor, secret, base_url, **kwargs):
        proof_values.append(secret)
        return await transient(vendor, secret, base_url, **kwargs)

    adapter.provision_transient_credential = capture_proof
    rows = [row for row in service.migration_scan()["items"] if row["backend"] == backend]
    assert len(rows) == 1 and rows[0]["proposed_action"] == "import"
    result = asyncio.run(service.migration_apply([rows[0]["id"]], clean_api_keys=True))
    assert result["applied"] == 1
    [source] = store.config.sources
    assert proof_values == [adapter.keys[source.credential_ref][2]] == [KEY]
    assert profile.read_bytes() == "# 保留偏好\r\n".encode()
    assert service.migration_journal.load() is None


@pytest.mark.parametrize("shape", ["claude-bearer", "codex-api", "codex-reference", "opencode-reference"])
def test_padded_persisted_key_reuses_the_proven_canonical_credential(home, tmp_path, shape):
    backend, profile = _seed(home, shape)
    service, store, adapter = _service(tmp_path, migration_home=home)
    vendor = "anthropic" if backend == "claude" else "openai"
    protocol = "anthropic" if backend == "claude" else "openai_responses"
    base_url = "https://fixture.example" if backend == "claude" else None
    scheme = "bearer" if backend == "claude" else None
    ref = asyncio.run(adapter.provision_credential(vendor, protocol, KEY, base_url, auth_scheme=scheme))
    store.config.sources.append(ModelHubSourceConfig.from_payload({
        "id": "src_paddingfixture", "kind": "api_key", "vendor": vendor,
        "display_name": "Existing fixture", "protocol": protocol,
        "base_url": base_url, "supply_channel": "hub", "billing": "metered",
        "state": {"status": "standby"}, "models": [], "credential_ref": ref,
    }))
    [row] = service.migration_scan()["items"]
    asyncio.run(service.migration_apply([row["id"]], clean_api_keys=True))
    assert len(adapter.provisioned) == len(store.config.sources) == 1
    assert store.config.sources[0].credential_ref == ref
    assert adapter.observed and not adapter.transient_refs
    assert profile.read_bytes() == "# 保留偏好\r\n".encode()


def test_padded_bearer_key_remains_usable_after_actual_http_proof_and_custody(home, tmp_path):
    async def scenario():
        async with _upstream("bearer") as (origin, requests):
            profile = home / ".profile"
            _write(profile, (
                f"export ANTHROPIC_AUTH_TOKEN=' \t{HTTP_KEY} \t'\n"
                f"export ANTHROPIC_BASE_URL='{origin}'\n"
            ))
            service, store, _ = _service(tmp_path, migration_home=home)
            runtime = _real_http_adapter(service, tmp_path)
            [row] = service.migration_scan()["items"]
            assert row["proposed_action"] == "import"
            await service.migration_apply([row["id"]], clean_api_keys=True)
            [source] = store.config.sources
            assert runtime.state_store.read_api_key(source.credential_ref) == HTTP_KEY
            assert await runtime.credential_auth_scheme(source.credential_ref) == "bearer"
            requests.clear()
            models = await runtime.discover_models("anthropic", "anthropic", origin, source.credential_ref)
            assert models
            assert any(headers.get("authorization") == f"Bearer {HTTP_KEY}" for _, headers in requests)
            assert all("x-api-key" not in headers for _, headers in requests)
            assert profile.read_bytes() == b""

    asyncio.run(scenario())


def test_whitespace_only_shell_assignments_are_not_credentials(home, tmp_path):
    profile = home / ".profile"
    _write(profile, "export OPENAI_API_KEY=' \t '\n")
    before = profile.read_bytes()
    service, _, adapter = _service(tmp_path, migration_home=home)
    assert service.migration_scan()["items"] == []
    assert not adapter.provisioned
    assert profile.read_bytes() == before


@pytest.mark.parametrize("selected_backend", ["claude", "codex", "opencode"])
def test_prepared_journal_contains_only_selected_credential_store_guards(home, tmp_path, selected_backend):
    unrelated = {
        "claude": (home / ".claude/.credentials.json", {
            "claudeAiOauth": {"accessToken": "fixture-unrelated-claude", "refreshToken": "fixture-refresh-claude"},
        }),
        "codex": (home / ".codex/auth.json", {
            "tokens": {"access_token": "fixture-unrelated-codex", "refresh_token": "fixture-refresh-codex"},
        }),
        "opencode": (home / ".local/share/opencode/auth.json", {
            "google": {"type": "oauth", "access": "fixture-unrelated-opencode", "refresh": "fixture-refresh-opencode"},
        }),
    }
    for backend, (path, payload) in unrelated.items():
        if backend != selected_backend:
            _write(path, json.dumps(payload))
    profile = home / ".profile"
    variable = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY", "opencode": "OPENROUTER_API_KEY"}
    _write(profile, f"export {variable[selected_backend]}='{KEY}'\n")
    service, _, adapter = _service(tmp_path, migration_home=home)
    saved = []
    save = service.migration_journal.save

    def record_prepared(record):
        if record["phase"] == "prepared":
            saved.append(json.loads(json.dumps(record)))
        save(record)

    service.migration_journal.save = record_prepared
    original = {path: path.read_bytes() for backend, (path, _) in unrelated.items() if backend != selected_backend}
    rows = [row for row in service.migration_scan()["items"] if row["backend"] == selected_backend]
    assert len(rows) == 1 and rows[0]["proposed_action"] == "import"
    asyncio.run(service.migration_apply([rows[0]["id"]], clean_api_keys=True))
    assert saved and adapter.provisioned
    for record in saved:
        edits = [NativeFileEdit.from_payload(payload) for payload in record["files"]]
        guarded = {edit.path for edit in edits}
        assert not guarded.intersection(original)
        assert unrelated[selected_backend][0] in guarded
        # Cross-backend config consumers, including absent layers, remain bound.
        assert {
            home / ".claude/settings.json",
            home / ".codex/config.toml",
            home / ".config/opencode/opencode.json",
        } <= guarded
        for edit in edits:
            assert b"fixture-unrelated-" not in (edit.before or b"")
            assert b"fixture-refresh-" not in (edit.before or b"")
    assert {path: path.read_bytes() for path in original} == original
    assert profile.read_bytes() == b""


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644, 0o750, 0o755])
def test_shell_mode_survives_journal_forward_reverse_and_replay(home, mode):
    path = home / ".bashrc"
    before = f"# 保留偏好\nexport OPENAI_API_KEY='{KEY}'\n".encode()
    path.write_bytes(before)
    path.chmod(mode)
    [profile] = [p for p in read_shell_profiles(home, frozenset({"OPENAI_API_KEY"})) if p.path == path]
    edit = cleanup_shell_profile(profile, {"OPENAI_API_KEY": KEY})
    assert edit.mode == mode
    for reverse in (False, False, True, True):
        NativeFileEdit.from_payload(edit.to_payload()).apply(reverse=reverse)
        assert path.read_bytes() == (before if reverse else "# 保留偏好\n".encode())
        assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize("reverse", [False, True])
def test_shell_mode_recovers_after_interrupted_atomic_publication(home, monkeypatch, reverse):
    path = home / ".bashrc"
    before = f"export OPENAI_API_KEY='{KEY}'\n# keep\n".encode()
    path.write_bytes(before)
    path.chmod(0o755)
    [profile] = [p for p in read_shell_profiles(home, frozenset({"OPENAI_API_KEY"})) if p.path == path]
    payload = cleanup_shell_profile(profile, {"OPENAI_API_KEY": KEY}).to_payload()
    replace = os.replace

    def interrupt_after_replace(source, target):
        replace(source, target)
        if Path(target) == path:
            raise InterruptedError("fixture interrupted after publication")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt_after_replace)
        with pytest.raises(InterruptedError):
            NativeFileEdit.from_payload(payload).apply()
    assert path.read_bytes() == b"# keep\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    NativeFileEdit.from_payload(payload).apply(reverse=reverse)
    assert path.read_bytes() == (before if reverse else b"# keep\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_compare_only_guard_does_not_claim_file_permissions(home, monkeypatch):
    path = home / ".bashrc"
    path.write_bytes(b"# untouched\n")
    path.chmod(0o755)
    guard = NativeFileEdit(path, path.read_bytes(), path.read_bytes())

    def forbidden(*args, **kwargs):
        pytest.fail("a compare-only guard cannot change permissions")

    monkeypatch.setattr(os, "chmod", forbidden)
    monkeypatch.setattr(os, "fchmod", forbidden)
    guard.apply()
    guard.apply(reverse=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


@pytest.mark.parametrize("replacement", ["before_open", "after_open"])
def test_mode_replay_rejects_replaced_content_without_chmod(home, monkeypatch, replacement):
    path = home / ".bashrc"
    path.write_bytes(b"before\n")
    path.chmod(0o644)
    edit = NativeFileEdit.plan(path, b"after\n")
    edit.apply()
    original_open = os.open
    changed = False

    def replace():
        private = home / "fixture-replacement"
        private.write_bytes(b"new private content\n")
        private.chmod(0o600)
        private.replace(path)

    def racing_open(target, flags, *args, **kwargs):
        nonlocal changed
        if Path(target) == path and not changed:
            changed = True
            if replacement == "before_open":
                replace()
            descriptor = original_open(target, flags, *args, **kwargs)
            if replacement == "after_open":
                replace()
            return descriptor
        return original_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(TakeoverStateError):
        NativeFileEdit.from_payload(edit.to_payload()).apply()
    assert path.read_bytes() == b"new private content\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("operation", ["check", "apply"])
def test_selected_shell_mode_change_invalidates_planned_edit(home, operation):
    path = home / ".bashrc"
    before = f"export OPENAI_API_KEY='{KEY}'\n# keep\n".encode()
    path.write_bytes(before)
    path.chmod(0o644)
    [profile] = [p for p in read_shell_profiles(home, frozenset({"OPENAI_API_KEY"})) if p.path == path]
    payload = cleanup_shell_profile(profile, {"OPENAI_API_KEY": KEY}).to_payload()
    path.chmod(0o600)
    with pytest.raises(TakeoverStateError):
        getattr(NativeFileEdit.from_payload(payload), operation)()
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("boundary", ["provision_credential", "ensure_installed"])
def test_service_rejects_tightened_shell_permissions_without_restoring_them(home, tmp_path, boundary):
    _, path = _seed(home, "codex-api")
    path.chmod(0o644)
    original = path.read_bytes()
    service, store, adapter = _service(tmp_path, migration_home=home)
    method = getattr(adapter, boundary)

    async def tighten(*args, **kwargs):
        result = await method(*args, **kwargs)
        path.chmod(0o600)
        return result

    setattr(adapter, boundary, tighten)
    [row] = service.migration_scan()["items"]
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply([row["id"]], clean_api_keys=True))
    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not store.config.sources
    assert not adapter.synced


def test_owned_mode_replay_fsyncs_already_restored_permissions(home, monkeypatch):
    path = home / ".bashrc"
    path.write_bytes(b"before\n")
    path.chmod(0o644)
    edit = NativeFileEdit.plan(path, b"after\n")
    fsync, replace = os.fsync, os.replace

    def interrupt_after_publication(source, target):
        replace(source, target)
        if Path(target) == path:
            raise InterruptedError("fixture publication before directory sync")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt_after_publication)
        with pytest.raises(InterruptedError):
            edit.apply()
    assert path.read_bytes() == b"after\n"
    flushed = []

    def record_fsync(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            flushed.append(os.fstat(descriptor).st_ino)
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)
    NativeFileEdit.from_payload(edit.to_payload()).apply()
    assert path.stat().st_ino in flushed


@pytest.mark.parametrize("failure", ["fchmod", "fsync", "replace"])
def test_unpublished_mode_failure_preserves_original_and_removes_temporary(home, monkeypatch, failure):
    path = home / ".bashrc"
    path.write_bytes(b"before\n")
    path.chmod(0o644)
    edit = NativeFileEdit.plan(path, b"after\n")
    baseline = set(home.iterdir())

    def fail(*args, **kwargs):
        raise OSError("fixture temporary publication failure")

    monkeypatch.setattr(os, failure, fail)
    with pytest.raises(OSError):
        edit.apply()
    assert path.read_bytes() == b"before\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert set(home.iterdir()) == baseline


def test_source_chmod_during_temporary_write_rejects_without_widening(home, monkeypatch):
    path = home / ".bashrc"
    path.write_bytes(b"before\n")
    path.chmod(0o644)
    edit = NativeFileEdit.plan(path, b"after\n")
    fsync = os.fsync
    baseline = set(home.iterdir())

    def tighten_source(descriptor):
        path.chmod(0o600)
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", tighten_source)
    with pytest.raises(TakeoverStateError):
        edit.apply()
    # Rollback cannot claim this untouched source's mode either.
    with pytest.raises(TakeoverStateError):
        edit.apply(reverse=True)
    assert path.read_bytes() == b"before\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert set(home.iterdir()) == baseline


def test_legacy_edit_default_mode_is_not_before_mode_evidence(home):
    path = home / ".bashrc"
    path.write_bytes(b"before\n")
    path.chmod(0o644)
    edit = NativeFileEdit.from_payload(NativeFileEdit(path, b"before\n", b"after\n").to_payload())
    edit.check()
    edit.apply()
    assert path.read_bytes() == b"after\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_reverse_uses_captured_before_mode_when_target_mode_differs(home):
    path = home / ".profile"
    path.write_bytes(b"before\n")
    path.chmod(0o640)
    edit = NativeFileEdit(path, b"before\n", b"after\n", 0o600, 0o640)
    edit.apply()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    NativeFileEdit.from_payload(edit.to_payload()).apply(reverse=True)
    assert path.read_bytes() == b"before\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    NativeFileEdit.from_payload(edit.to_payload()).apply(reverse=True)


def test_captured_private_shell_mode_survives_restrictive_umask(home):
    path = home / ".profile"
    path.write_bytes(b"before\n")
    path.chmod(0o600)
    edit = NativeFileEdit.plan(path, b"after\n")
    previous_mask = os.umask(0o200)
    try:
        edit.apply()
        NativeFileEdit.from_payload(edit.to_payload()).apply(reverse=True)
    finally:
        os.umask(previous_mask)
    assert path.read_bytes() == b"before\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_deleted_target_replay_completes_directory_durability(home, monkeypatch):
    path = home / ".fixture"
    path.write_bytes(b"fixture\n")
    edit = NativeFileEdit.plan(path, None)
    edit.apply()
    flushed = []
    monkeypatch.setattr(migration_journal, "_fsync_directory", flushed.append)
    edit.apply()
    assert flushed == [home]


@pytest.mark.parametrize("builtin", ["mapfile", "readarray"])
@pytest.mark.parametrize("arguments", [
    "OPENAI_API_KEY", "-t OPENAI_API_KEY", "-tn2 OPENAI_API_KEY",
    "-d '' -n 2 -O 0 -s 1 -t -u 3 -C callback -c 1 OPENAI_API_KEY",
    "-d: -n2 -O0 -s1 -u3 -Ccallback -c1 -- OPENAI_API_KEY",
])
@pytest.mark.parametrize("prior_literal", [False, True])
def test_array_readers_are_dynamic_credential_writers(home, builtin, arguments, prior_literal):
    path = home / ".bashrc"
    text = (f"export OPENAI_API_KEY='{KEY}'\n" if prior_literal else "")
    text += f"{builtin} {arguments} < /fixture/not-read\n"
    path.write_text(text)
    [profile] = [p for p in read_shell_profiles(home, frozenset({"OPENAI_API_KEY"})) if p.path == path]
    assert "OPENAI_API_KEY" not in profile.values
    assert any(issue.reason == "dynamic_shell" and "OPENAI_API_KEY" in issue.names for issue in profile.issues)
    with pytest.raises(TakeoverStateError):
        cleanup_shell_profile(profile, {"OPENAI_API_KEY": KEY})
    assert path.read_text() == text


@pytest.mark.parametrize("builtin", ["mapfile", "readarray"])
@pytest.mark.parametrize("arguments", [
    "-d OPENAI_API_KEY unrelated",
    "-C OPENAI_API_KEY -c 1 unrelated",
    "-t unrelated < OPENAI_API_KEY",
])
def test_array_reader_option_operands_and_redirections_are_not_destinations(home, builtin, arguments):
    path = home / ".bashrc"
    path.write_text(f"{builtin} {arguments}\nexport OPENAI_API_KEY='{KEY}'\n")
    [profile] = [p for p in read_shell_profiles(home, frozenset({"OPENAI_API_KEY"})) if p.path == path]
    assert profile.values == {"OPENAI_API_KEY": KEY}
    assert profile.issues == ()


@pytest.mark.parametrize("writer", [
    "builtin mapfile -t OPENAI_API_KEY",
    "command readarray -- OPENAI_API_KEY",
    "if true; then mapfile -t OPENAI_API_KEY; fi",
    "f() { readarray OPENAI_API_KEY; }",
    'echo "$(mapfile OPENAI_API_KEY)"',
    "eval 'readarray OPENAI_API_KEY'",
    "mapfile -C 'export OPENAI_API_KEY=dynamic' unrelated",
    "readarray -tC'export OPENAI_API_KEY=dynamic' unrelated",
])
def test_array_reader_nested_and_explicit_callback_writers_are_blocked(home, tmp_path, writer):
    path = home / ".bashrc"
    text = f"export OPENAI_API_KEY='{KEY}'\n{writer}\n"
    path.write_text(text)
    service, store, adapter = _service(tmp_path, migration_home=home)
    [row] = service.migration_scan()["items"]
    assert row["proposed_action"] == "reauth" and not row["selected"]
    assert row["notes_key"] == "settings.models.migration.blocked.dynamic_shell"
    assert path.read_text() == text
    assert not adapter.provisioned and not store.config.sources


@pytest.mark.parametrize("builtin", ["mapfile", "readarray"])
def test_array_reader_default_destination_is_also_a_persisted_reference(home, builtin):
    path = home / ".bashrc"
    path.write_text(f"MAPFILE=fixture\n{builtin} -t < /fixture/not-read\n")
    [profile] = [p for p in read_shell_profiles(home, frozenset({"MAPFILE"})) if p.path == path]
    assert profile.values == {}
    assert profile.issues[0].names == ("MAPFILE",)
