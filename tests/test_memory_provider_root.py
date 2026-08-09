from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.memory.provider_root import (
    PROVIDER_ROOT_CONTROL_FILES,
    ROOT_SENTINEL_FILENAME,
    ProviderRoot,
    ProviderRootError,
    ProviderRootMetadata,
    ProviderRootState,
)


def _metadata(
    root_format: str = "everos-1.0",
    *,
    fingerprint: str = "artifact-1.0",
    compatible: frozenset[str] | None = None,
) -> ProviderRootMetadata:
    return ProviderRootMetadata(
        provider_root_format=root_format,
        artifact_fingerprint=fingerprint,
        compatible_provider_root_formats=(
            compatible if compatible is not None else frozenset({root_format})
        ),
    )


def _owner(tmp_path: Path) -> tuple[ProviderRoot, SimpleNamespace]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    meta = SimpleNamespace(provider_root_id="root-id")
    return ProviderRoot(home / "memory" / "everos-root", effective_home=home), meta


def _sentinel(root: ProviderRoot) -> dict[str, object]:
    return json.loads(
        (root.path / ROOT_SENTINEL_FILENAME).read_text(encoding="utf-8")
    )


def test_provider_root_inspect_reports_absent_root(tmp_path: Path) -> None:
    root, _meta = _owner(tmp_path)

    assert root.inspect(_metadata()) == ProviderRootState(exists=False)


def test_provider_root_ensure_creates_private_owned_root_and_sentinel(
    tmp_path: Path,
) -> None:
    root, meta = _owner(tmp_path)
    metadata = _metadata()

    root.ensure(meta, metadata)

    assert root.inspect(metadata) == ProviderRootState(
        exists=True,
        provider_root_format="everos-1.0",
        empty=True,
    )
    assert os.stat(root.path).st_mode & 0o777 == 0o700
    assert os.stat(root.path / ROOT_SENTINEL_FILENAME).st_mode & 0o777 == 0o600
    assert _sentinel(root) == {
        "schema_version": 1,
        "provider_root_id": meta.provider_root_id,
        "provider_id": "everos",
        "provider_root_format": "everos-1.0",
        "created_by_artifact_fingerprint": "artifact-1.0",
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda root: root.chmod(0o755), "mode mismatch"),
        (
            lambda root: (root / ROOT_SENTINEL_FILENAME).chmod(0o644),
            "sentinel is invalid",
        ),
        (
            lambda root: (root / ROOT_SENTINEL_FILENAME).write_text(
                "{}", encoding="utf-8"
            ),
            "sentinel is invalid",
        ),
    ],
)
def test_provider_root_inspect_rejects_invalid_root_policy(
    tmp_path: Path,
    mutate,
    message: str,
) -> None:
    root, meta = _owner(tmp_path)
    metadata = _metadata()
    root.ensure(meta, metadata)
    mutate(root.path)

    with pytest.raises(ProviderRootError, match=message):
        root.inspect(metadata)


def test_provider_root_inspect_rejects_owner_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root, meta = _owner(tmp_path)
    metadata = _metadata()
    root.ensure(meta, metadata)
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        pytest.skip("platform does not expose file ownership")
    monkeypatch.setattr(os, "getuid", lambda: getuid() + 1)

    with pytest.raises(ProviderRootError, match="owner mismatch"):
        root.inspect(metadata)


def test_provider_root_inspect_rejects_symlinked_sentinel(tmp_path: Path) -> None:
    root, meta = _owner(tmp_path)
    metadata = _metadata()
    root.ensure(meta, metadata)
    sentinel = root.path / ROOT_SENTINEL_FILENAME
    sentinel.unlink()
    outside = tmp_path / "outside-sentinel"
    outside.write_text("{}", encoding="utf-8")
    outside.chmod(0o600)
    sentinel.symlink_to(outside)

    with pytest.raises(ProviderRootError, match="sentinel is unsafe"):
        root.inspect(metadata)


def test_provider_root_inspect_owns_format_and_control_file_semantics(
    tmp_path: Path,
) -> None:
    root, meta = _owner(tmp_path)
    active = _metadata()
    root.ensure(meta, active)
    (root.path / "everos.toml").write_text("[memory]\n", encoding="utf-8")
    (root.path / "ome.toml").write_text("[strategies]\n", encoding="utf-8")

    assert root.inspect(_metadata("everos-2.0")) == ProviderRootState(
        exists=True,
        provider_root_format="everos-1.0",
        empty=True,
    )
    assert root.has_data() is False
    assert PROVIDER_ROOT_CONTROL_FILES == frozenset(
        {ROOT_SENTINEL_FILENAME, "everos.toml", "ome.toml"}
    )

    (root.path / "vectors").write_bytes(b"provider data")
    with pytest.raises(ProviderRootError, match="format is incompatible"):
        root.inspect(_metadata("everos-2.0"))
    assert root.inspect(
        _metadata("everos-2.0", compatible=frozenset({"everos-1.0", "everos-2.0"}))
    ).empty is False
    assert root.has_data() is True


def test_provider_root_ensure_rejects_unsentinelled_nonempty_root(
    tmp_path: Path,
) -> None:
    root, meta = _owner(tmp_path)
    root.path.parent.mkdir(mode=0o700)
    root.path.mkdir(mode=0o700)
    (root.path / "vectors").write_bytes(b"provider data")

    with pytest.raises(ProviderRootError, match="root is not empty"):
        root.ensure(meta, _metadata())


def test_provider_root_activate_empty_format_returns_working_rollback(
    tmp_path: Path,
) -> None:
    root, meta = _owner(tmp_path)
    active = _metadata()
    candidate = _metadata("everos-2.0", fingerprint="artifact-2.0")
    root.ensure(meta, active)
    (root.path / "everos.toml").write_text("[memory]\n", encoding="utf-8")
    (root.path / "ome.toml").write_text("[strategies]\n", encoding="utf-8")

    rollback = root.activate_empty_format(meta, candidate)

    assert rollback is not None
    assert _sentinel(root)["provider_root_format"] == "everos-2.0"
    assert _sentinel(root)["created_by_artifact_fingerprint"] == "artifact-2.0"

    rollback.rollback()

    assert _sentinel(root)["provider_root_format"] == "everos-1.0"
    assert _sentinel(root)["created_by_artifact_fingerprint"] == "artifact-1.0"
    assert root.inspect(active).empty is True


def test_provider_root_activation_restores_sentinel_after_postwrite_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root, meta = _owner(tmp_path)
    active = _metadata()
    candidate = _metadata("everos-2.0", fingerprint="artifact-2.0")
    root.ensure(meta, active)
    original_verify = root._verify
    failed = False

    def fail_candidate_once(*args, **kwargs) -> None:
        nonlocal failed
        original_verify(*args, **kwargs)
        if _sentinel(root)["provider_root_format"] == "everos-2.0" and not failed:
            failed = True
            raise RuntimeError("injected post-write verification failure")

    monkeypatch.setattr(root, "_verify", fail_candidate_once)

    with pytest.raises(RuntimeError, match="post-write verification failure"):
        root.activate_empty_format(meta, candidate)

    assert failed is True
    assert _sentinel(root)["provider_root_format"] == "everos-1.0"
    assert _sentinel(root)["created_by_artifact_fingerprint"] == "artifact-1.0"


def test_provider_root_recreate_empty_removes_children_but_preserves_root(
    tmp_path: Path,
) -> None:
    root, meta = _owner(tmp_path)
    metadata = _metadata()
    root.ensure(meta, metadata)
    identity = root.path.stat().st_ino
    nested = root.path / "vectors" / "nested"
    nested.mkdir(parents=True)
    (nested / "data").write_bytes(b"provider data")
    (root.path / "everos.toml").write_text("generated", encoding="utf-8")

    root.recreate_empty(meta, metadata)

    assert root.path.stat().st_ino == identity
    assert {entry.name for entry in root.path.iterdir()} == {
        ROOT_SENTINEL_FILENAME
    }
    assert root.inspect(metadata).empty is True


def test_provider_root_rejects_symlinked_path_chain(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    (home / "memory").symlink_to(outside, target_is_directory=True)
    root = ProviderRoot(home / "memory" / "everos-root", effective_home=home)
    meta = SimpleNamespace(provider_root_id="root-id")

    with pytest.raises(ProviderRootError, match="chain contains a symlink"):
        root.ensure(meta, _metadata())
