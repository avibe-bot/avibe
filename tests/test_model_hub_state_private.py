"""Model Hub's engine secrets and takeover journal stay owner-private, Windows included.

POSIX keeps its 0o700/0o600 coverage beside each owner. These cases pin what
Windows adds: modes mean nothing there, so engine start and an API-key Source
depend on the owner-private DACL instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from config.v2_config import ModelHubConfig
from core import private_fs
from core.handlers.model_hub.migration_journal import NativeFileEdit, NativeTakeoverJournal
from tests.test_private_fs import restore_windows_inheritance, widen
from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore

windows_only = pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACLs")


@pytest.fixture
def root(tmp_path):
    try:
        yield tmp_path / "model-hub"
    finally:
        restore_windows_inheritance(tmp_path)


def _private(path: Path) -> bool:
    return private_fs.is_private(path, path.lstat().st_mode)


@windows_only
def test_windows_engine_start_and_api_key_source_keep_secrets_private(root):
    store = EngineStateStore(root / "state")
    instance_dir, secrets = store.prepare_instance("install-1")
    # The second start reads the runtime secret back through its private-file check.
    assert store.prepare_instance("install-1") == (instance_dir, secrets)
    reference = store.store_api_key("sk-synthetic-windows", base_url="https://example.invalid/v1")
    assert store.read_api_key(reference) == "sk-synthetic-windows"

    # The engine writes its grants into the auth directory without asking for
    # any access rule; they must pass the audit every engine start runs.
    (store.auth_dir / "codex-synthetic.json").write_text("{}", encoding="utf-8")
    store.audit_auth_permissions()
    for path in (store.root, instance_dir, instance_dir / "runtime-secrets.json", instance_dir / "logs", store.auth_dir):
        assert _private(path), path


@windows_only
def test_windows_widened_secrets_are_refused_and_auth_files_repaired_only_when_enforced(root):
    store = EngineStateStore(root / "state")
    store.prepare_instance("install-1")
    reference = store.store_api_key("sk-synthetic-windows", base_url="https://example.invalid/v1")
    widen(store.root / "credentials" / f"{reference}.json")
    with pytest.raises(EngineStateError, match="credential permissions are unsafe"):
        store.read_api_key(reference)

    grant = store.auth_dir / "codex-synthetic.json"
    grant.write_text("{}", encoding="utf-8")
    widen(grant)
    with pytest.raises(EngineStateError, match="auth credential permissions are unsafe"):
        store.audit_auth_permissions()
    store.audit_auth_permissions(enforce=True)
    assert _private(grant)


def test_takeover_journal_repairs_its_directory_and_publishes_a_new_native_file(root):
    """A takeover directory an earlier build left non-private is repaired, not refused.

    Released Windows builds created it with the profile's inherited DACL before
    the check that always failed there, so refusing would block every later
    takeover on those hosts.
    """

    journal = NativeTakeoverJournal(root / "native-takeover" / "current.json")
    journal.path.parent.mkdir(parents=True)
    widen(journal.path.parent)
    native = root / "native" / "settings.json"
    native.parent.mkdir()
    edit = NativeFileEdit.plan(native, b'{"model":"hub"}\n')
    journal.save({
        "version": 1, "phase": "prepared", "items": [],
        "backends": ["claude"], "files": [edit.to_payload()],
        "source_ids": [], "credentials": [],
        "previous": ModelHubConfig().to_payload(),
        "updated": ModelHubConfig().to_payload(),
    })
    assert _private(journal.path.parent) and _private(journal.path)

    loaded = NativeFileEdit.from_payload(journal.load()["files"][0])
    loaded.apply()
    loaded.check(applied=True)
    assert native.read_bytes() == b'{"model":"hub"}\n'
    loaded.apply(reverse=True)
    assert not native.exists()
