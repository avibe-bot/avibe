"""Connection ownership reads never repair engine state or disclose credentials."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from vibe.model_hub_runtime.state import EngineStateStore


def _target(kind="api_key"):
    return {
        "source_id": "src_connection01", "kind": kind, "vendor": "openai",
        "protocol": "openai_responses", "base_url": None,
    }


def _seed(store, kind):
    if kind == "api_key":
        return store.store_api_key("fixture-private-key", vendor="openai", protocol="openai_responses")
    return store.bind_oauth_credential("src_connection01", "openai", "fixture.json")


@pytest.mark.parametrize("kind", ["api_key", "subscription"])
def test_owned_record_returns_only_bool_without_reading_oauth_auth_or_repairing(tmp_path, monkeypatch, caplog, kind):
    store = EngineStateStore(tmp_path / "engine-state")
    credential_ref = _seed(store, kind)
    before = {path: (path.stat().st_mode, path.read_bytes()) for path in store.root.rglob("*") if path.is_file()}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Read-only ownership must not repair or open the OAuth grant")

    monkeypatch.setattr(store, "_ensure_private_dir", forbidden)
    monkeypatch.setattr(store, "credential_metadata_if_present", forbidden)
    monkeypatch.setattr(store, "_decode_oauth_payload", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "chmod", forbidden)
    result = store.has_current_source_credential(credential_ref, **_target(kind))
    assert result is True
    assert {path: (path.stat().st_mode, path.read_bytes()) for path in before} == before
    assert not store.auth_dir.exists()
    assert "fixture-private-key" not in caplog.text


def test_missing_record_does_not_create_engine_directories(tmp_path):
    store = EngineStateStore(tmp_path / "absent" / "engine-state")
    assert store.has_current_source_credential("cred_missing01", **_target()) is False
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("kind", ["api_key", "subscription"])
@pytest.mark.parametrize("field,value", [
    ("vendor", "anthropic"), ("kind", "custom"), ("source_id", "../escape"),
    ("protocol", "invalid"), ("base_url", "file:///outside"),
])
def test_invalid_or_mismatched_source_cannot_borrow_record(tmp_path, kind, field, value):
    store = EngineStateStore(tmp_path / "engine")
    credential_ref = _seed(store, kind)
    assert store.has_current_source_credential(credential_ref, **{**_target(kind), field: value}) is False


@pytest.mark.parametrize("changes", [
    {"kind": "subscription"},
    {"protocol": "openai_chat"},
    {"base_url": "https://other.example"},
])
def test_api_key_binding_requires_exact_kind_protocol_and_endpoint(tmp_path, changes):
    store = EngineStateStore(tmp_path / "engine")
    credential_ref = _seed(store, "api_key")
    assert store.has_current_source_credential(credential_ref, **{**_target(), **changes}) is False


@pytest.mark.parametrize("changes", [
    {"kind": "api_key"},
    {"source_id": "src_other0001"},
    {"base_url": "https://other.example"},
])
def test_oauth_binding_requires_its_source_identity(tmp_path, changes):
    store = EngineStateStore(tmp_path / "engine")
    credential_ref = _seed(store, "subscription")
    assert store.has_current_source_credential(credential_ref, **{**_target("subscription"), **changes}) is False


@pytest.mark.parametrize("credential_ref", ["../outside", "cred_../../outside", "", "cred_short", None, []])
def test_reference_is_confined_without_creating_any_path(tmp_path, credential_ref):
    store = EngineStateStore(tmp_path / "absent")
    assert store.has_current_source_credential(credential_ref, **_target()) is False
    assert not store.root.exists()


@pytest.mark.parametrize("payload", [
    "{invalid", "[]", "null", "{}",
    '{"kind":"api_key","vendor":"openai","protocol":"openai_responses","base_url":null,"value":""}',
    '{"kind":"api_key","vendor":"openai","protocol":"openai_responses","base_url":null,"value":[]}',
    '{"kind":"api_key","vendor":"anthropic","protocol":"openai_responses","base_url":null,"value":"fixture-private-key"}',
])
def test_bad_record_is_false_and_is_not_repaired_or_logged(tmp_path, caplog, payload):
    store = EngineStateStore(tmp_path / "engine")
    credential_ref = _seed(store, "api_key")
    path = store.root / "credentials" / f"{credential_ref}.json"
    path.write_text(payload)
    before = path.stat()
    assert store.has_current_source_credential(credential_ref, **_target()) is False
    assert path.read_text() == payload and path.stat().st_mtime_ns == before.st_mtime_ns
    assert "fixture-private-key" not in caplog.text
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("location", ["root", "credentials", "record"])
@pytest.mark.parametrize("unsafe", ["permissions", "symlink"])
def test_unsafe_record_or_parent_is_not_followed_or_repaired(tmp_path, location, unsafe):
    store = EngineStateStore(tmp_path / "engine")
    credential_ref = _seed(store, "api_key")
    target = {
        "root": store.root,
        "credentials": store.root / "credentials",
        "record": store.root / "credentials" / f"{credential_ref}.json",
    }[location]
    if unsafe == "permissions":
        target.chmod(0o755 if target.is_dir() else 0o644)
        before = target.stat().st_mode
    else:
        parked = target.with_name(target.name + "-parked")
        target.rename(parked)
        target.symlink_to(parked, target_is_directory=parked.is_dir())
    assert store.has_current_source_credential(credential_ref, **_target()) is False
    if unsafe == "permissions":
        assert target.stat().st_mode == before
    else:
        assert target.is_symlink()


@pytest.mark.parametrize("state,ready", [(None, True), ("active", True), ("staged", False), ("unknown", False)])
def test_oauth_binding_is_not_ready_while_staged(tmp_path, state, ready):
    store = EngineStateStore(tmp_path / "engine")
    credential_ref = _seed(store, "subscription")
    path = store.root / "credentials" / f"{credential_ref}.json"
    payload = json.loads(path.read_text())
    if state is not None:
        payload["activation_state"] = state
    path.write_text(json.dumps(payload))
    assert store.has_current_source_credential(credential_ref, **_target("subscription")) is ready
    assert not store.auth_dir.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("auth_name", ["../outside.json", "/absolute.json", "not-json", "", None, []])
def test_oauth_binding_requires_safe_auth_name_but_does_not_open_it(tmp_path, auth_name):
    store = EngineStateStore(tmp_path / "engine")
    credential_ref = _seed(store, "subscription")
    path = store.root / "credentials" / f"{credential_ref}.json"
    payload = json.loads(path.read_text())
    payload["auth_name"] = auth_name
    path.write_text(json.dumps(payload))
    assert store.has_current_source_credential(credential_ref, **_target("subscription")) is False
    assert not store.auth_dir.exists()


def test_credential_read_error_is_a_secret_free_false(tmp_path, monkeypatch, caplog):
    store = EngineStateStore(tmp_path / "engine")
    credential_ref = _seed(store, "api_key")

    def denied(*_args, **_kwargs):
        raise PermissionError("fixture-private-key")

    monkeypatch.setattr(store, "_read_private_bytes", denied)
    assert store.has_current_source_credential(credential_ref, **_target()) is False
    assert "fixture-private-key" not in caplog.text
