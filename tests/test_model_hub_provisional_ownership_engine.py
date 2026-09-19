from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

import vibe.model_hub_runtime.state as state_module
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore


class _ProvisionSupervisor:
    def __init__(self, state_store: EngineStateStore) -> None:
        self.state_store = state_store


def _oauth_material() -> dict[str, str]:
    return {
        "type": "codex",
        "access_token": "oauth-access-fixture",
        "refresh_token": "oauth-refresh-fixture",
        "account_id": "oauth-account-fixture",
    }


def _namespace_paths(
    store: EngineStateStore,
    credential_ref: str,
) -> tuple[Path, Path, Path, Path]:
    return store._credential_namespace_paths(credential_ref)


def _assert_no_secret_bytes(paths: tuple[Path, ...], *secrets: str) -> None:
    for path in paths:
        if path.exists():
            contents = path.read_bytes()
            for secret in secrets:
                assert secret.encode() not in contents


def test_api_key_callback_is_before_secret_and_releases_oauth_namespace(
    tmp_path: Path,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    observed: list[tuple[str, tuple[Path, ...]]] = []

    def on_reserved(ref: str) -> None:
        paths = _namespace_paths(store, ref)
        observed.append((ref, paths))
        assert all(path.exists() for path in paths)
        for path in paths:
            assert json.loads(path.read_text()) == {
                "credential_ref": ref,
                "kind": "reservation",
            }
        _assert_no_secret_bytes(paths, "api-secret-fixture")

    ref = store.store_api_key(
        "api-secret-fixture",
        vendor="openai",
        protocol="openai_chat",
        base_url="https://api.example.test/v1",
        on_reserved=on_reserved,
    )

    assert len(observed) == 1
    assert observed[0][0] == ref
    assert store.read_api_key(ref) == "api-secret-fixture"
    credential_path, credential_tmp, stage_path, stage_tmp = _namespace_paths(store, ref)
    assert credential_path.exists()
    assert not credential_tmp.exists()
    assert not stage_path.exists()
    assert not stage_tmp.exists()


def test_oauth_callback_is_before_any_grant_bytes(
    tmp_path: Path,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    observed: list[tuple[str, tuple[Path, ...]]] = []

    def on_reserved(ref: str) -> None:
        paths = _namespace_paths(store, ref)
        observed.append((ref, paths))
        assert all(path.exists() for path in paths)
        for path in paths:
            assert json.loads(path.read_text()) == {
                "credential_ref": ref,
                "kind": "reservation",
            }
        _assert_no_secret_bytes(
            paths,
            "oauth-access-fixture",
            "oauth-refresh-fixture",
        )

    ref = store.stage_oauth_credential(
        "src_fixture123",
        "openai",
        "fixture-auth.json",
        _oauth_material(),
        on_reserved=on_reserved,
    )

    assert observed == [(ref, observed[0][1])]
    metadata = store.credential_metadata(ref)
    assert metadata["activation_state"] == "staged"
    stage_path = _namespace_paths(store, ref)[2]
    assert json.loads(stage_path.read_text())["refresh_token"] == "oauth-refresh-fixture"
    assert not list(store.auth_dir.glob("*.json"))


@pytest.mark.parametrize("kind", ["api", "oauth"])
def test_callback_failure_leaves_no_provisional_material(
    tmp_path: Path,
    kind: str,
) -> None:
    store = EngineStateStore(tmp_path / "engine")

    def fail(_ref: str) -> None:
        raise RuntimeError("fixture reservation callback failed")

    if kind == "api":
        with pytest.raises(RuntimeError, match="callback failed"):
            store.store_api_key("api-secret-fixture", on_reserved=fail)
    else:
        with pytest.raises(EngineStateError, match="unable to stage OAuth credential"):
            store.stage_oauth_credential(
                "src_fixture123",
                "openai",
                "fixture-auth.json",
                _oauth_material(),
                on_reserved=fail,
            )

    assert not list((store.root / "credentials").glob("*"))
    assert not list(store.oauth_staging_dir.glob("*"))


def test_api_key_ref_collision_in_orphaned_oauth_namespace_is_before_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_token = "a" * 32
    monkeypatch.setattr(state_module.secrets, "token_hex", lambda _length: fixed_token)
    store = EngineStateStore(tmp_path / "engine")
    ref = f"cred_{fixed_token}"
    stage_path = _namespace_paths(store, ref)[2]
    stage_path.parent.mkdir(parents=True, exist_ok=True)
    old_stage = b'{"refresh_token":"old-stage-fixture"}\n'
    stage_path.write_bytes(old_stage)
    stage_path.chmod(0o600)
    callback_called = False

    def on_reserved(_ref: str) -> None:
        nonlocal callback_called
        callback_called = True

    with pytest.raises(EngineStateError, match="reference collision"):
        store.store_api_key("new-api-secret-fixture", on_reserved=on_reserved)

    assert not callback_called
    assert stage_path.read_bytes() == old_stage
    credential_path, credential_tmp, _, stage_tmp = _namespace_paths(store, ref)
    assert not credential_path.exists()
    assert not credential_tmp.exists()
    assert not stage_tmp.exists()


def test_revoke_missing_metadata_removes_exact_secret_temps_only(
    tmp_path: Path,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    ref = "cred_" + "b" * 32
    paths = _namespace_paths(store, ref)
    for path in paths:
        store._reserve_path(path, ref)
    paths[1].write_bytes(b'{"value":"orphan-api-secret-fixture"}\n')
    paths[1].chmod(0o600)
    paths[3].write_bytes(b'{"refresh_token":"orphan-oauth-secret-fixture"}\n')
    paths[3].chmod(0o600)
    unrelated = paths[0].with_name("cred_other.json")
    unrelated.write_bytes(b'{"value":"unrelated-fixture"}\n')
    unrelated.chmod(0o600)

    store.revoke_credential(ref)

    assert all(not path.exists() for path in paths)
    assert unrelated.exists()


def test_replace_failure_leaves_deterministic_temp_for_exact_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    ref = "cred_" + "e" * 32
    credential_path, credential_tmp, stage_path, stage_tmp = _namespace_paths(store, ref)
    for path in (credential_path, credential_tmp, stage_path, stage_tmp):
        store._reserve_path(path, ref)

    def fail_replace(_source: str | bytes | os.PathLike[str], _target: str | bytes | os.PathLike[str]) -> None:
        raise OSError(errno.EIO, "fixture rename failure")

    monkeypatch.setattr(state_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="fixture rename failure"):
        store._write_reserved_json(
            credential_path,
            {"kind": "api_key", "value": "rename-window-secret-fixture"},
            temporary_path=credential_tmp,
            credential_ref=ref,
        )

    assert credential_tmp.exists()
    assert b"rename-window-secret-fixture" in credential_tmp.read_bytes()
    store.revoke_credential(ref)
    assert all(not path.exists() for path in (credential_path, credential_tmp, stage_path, stage_tmp))


@pytest.mark.asyncio
async def test_orphan_cleanup_does_not_touch_watched_files(
    tmp_path: Path,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    ref = "cred_" + "c" * 32
    paths = _namespace_paths(store, ref)
    for path in paths:
        store._reserve_path(path, ref)
    paths[1].write_bytes(b'{"value":"orphan-secret-fixture"}\n')
    paths[1].chmod(0o600)
    store.audit_auth_permissions()
    watched = store.auth_dir / "unrelated-auth.json"
    watched.write_text('{"type":"codex","refresh_token":"keep-fixture"}\n')
    watched.chmod(0o600)
    adapter = CLIProxyEngineAdapter(
        supervisor=_ProvisionSupervisor(store),  # type: ignore[arg-type]
        state_store=store,
    )

    assert await adapter.cleanup_orphaned_oauth_material(ref) is True
    assert watched.exists()
    assert all(not path.exists() for path in paths)


@pytest.mark.asyncio
async def test_transient_provision_forwards_write_ahead_callback(
    tmp_path: Path,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    adapter = CLIProxyEngineAdapter(
        supervisor=_ProvisionSupervisor(store),  # type: ignore[arg-type]
        state_store=store,
    )
    observed: list[str] = []

    def on_reserved(ref: str) -> None:
        observed.append(ref)
        _assert_no_secret_bytes(
            _namespace_paths(store, ref),
            "transient-secret-fixture",
        )

    ref = await adapter.provision_transient_credential(
        "openai",
        "transient-secret-fixture",
        None,
        on_reserved=on_reserved,
    )

    assert observed == [ref]
    assert store.read_api_key(ref) == "transient-secret-fixture"


@pytest.mark.asyncio
async def test_owned_provision_worker_joins_after_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    adapter = CLIProxyEngineAdapter(
        supervisor=_ProvisionSupervisor(store),  # type: ignore[arg-type]
        state_store=store,
    )
    original_store_api_key = store.store_api_key
    started = threading.Event()
    release = threading.Event()
    callback_refs: list[str] = []

    def delayed_store_api_key(*args: Any, **kwargs: Any) -> str:
        started.set()
        assert release.wait(2)
        return original_store_api_key(*args, **kwargs)

    monkeypatch.setattr(store, "store_api_key", delayed_store_api_key)
    task = asyncio.create_task(
        adapter.provision_credential(
            "openai",
            "openai_chat",
            "owned-secret-fixture",
            None,
            on_reserved=callback_refs.append,
        )
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(callback_refs) == 1
    assert store.read_api_key(callback_refs[0]) == "owned-secret-fixture"


@pytest.mark.asyncio
async def test_cleanup_retries_directory_fsync_after_unlink_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    ref = "cred_" + "d" * 32
    paths = _namespace_paths(store, ref)
    for path in paths:
        store._reserve_path(path, ref)
    original_fsync = state_module.os.fsync
    failed = False

    def fail_once(descriptor: int) -> None:
        nonlocal failed
        if not failed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            failed = True
            raise OSError(errno.EIO, "fixture directory flush failure")
        original_fsync(descriptor)

    monkeypatch.setattr(state_module.os, "fsync", fail_once)
    with pytest.raises(EngineStateError, match="unable to remove engine state file"):
        store.revoke_credential(ref)
    assert failed
    assert not paths[2].exists()

    monkeypatch.setattr(state_module.os, "fsync", original_fsync)
    store.revoke_credential(ref)
    assert all(not path.exists() for path in paths)
