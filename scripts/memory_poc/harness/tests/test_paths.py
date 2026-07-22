from __future__ import annotations

from pathlib import Path

import pytest

from memory_poc.errors import HarnessError
from memory_poc.paths import create_owner_directory, ensure_owner_directory, write_private_text


def test_anchored_runtime_creation_rejects_a_symlinked_ancestor(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (checkout / ".runtime").symlink_to(outside, target_is_directory=True)

    with pytest.raises(HarnessError, match="unsafe_runtime_directory"):
        ensure_owner_directory(checkout / ".runtime" / "memory-poc", anchor=checkout)

    assert not (outside / "memory-poc").exists()


def test_private_write_replaces_an_internal_symlink_without_following_it(tmp_path: Path) -> None:
    root = ensure_owner_directory(tmp_path / "root")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    target = root / "generated.toml"
    target.symlink_to(outside)

    write_private_text(target, "inside\n", anchor=root)

    assert outside.read_text(encoding="utf-8") == "outside"
    assert target.read_text(encoding="utf-8") == "inside\n"
    assert not target.is_symlink()


def test_create_owner_directory_is_exclusive(tmp_path: Path) -> None:
    root = ensure_owner_directory(tmp_path / "root")
    run_dir = root / "runs" / "r1"

    create_owner_directory(run_dir, anchor=root)

    assert run_dir.is_dir()
    assert run_dir.stat().st_mode & 0o777 == 0o700
    with pytest.raises(HarnessError, match="runtime_directory_exists"):
        create_owner_directory(run_dir, anchor=root)
