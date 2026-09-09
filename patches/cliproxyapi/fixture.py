"""Materialize and verify the frozen Avibe fixture, never the mutable worktree."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile

from isolation import validate_state_root


def source_digest(root: Path) -> str:
    """Bind every exported file/link and reject links leaving the export."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        name = path.relative_to(root).as_posix().encode()
        if stat.S_ISLNK(mode):
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"Fixture link leaves its source root: {name!r}")
            kind, content = b"link", os.readlink(path).encode()
        elif stat.S_ISREG(mode):
            kind, content = b"file", path.read_bytes()
        elif stat.S_ISDIR(mode):
            kind, content = b"dir", b""
        else:
            raise ValueError(f"Unsupported fixture file: {name!r}")
        for value in (name, kind, content):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def verify_fixture(root: Path, expected: str) -> None:
    if source_digest(root) != expected:
        raise RuntimeError("Frozen Avibe fixture changed; preserve the evidence and use a fresh export.")


def export_fixture(repository: Path, state: Path, receipt: dict) -> tuple[Path, dict]:
    """An exact commit archive ignores staged, dirty and untracked worktree files."""
    state = validate_state_root(state)
    base = receipt["avibe_fixture_base"]
    tree = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", f"{base}^{{tree}}"], text=True,
    ).strip()
    if tree != receipt["avibe_fixture_tree"]:
        raise RuntimeError("Wrong Avibe fixture tree.")
    archive = subprocess.check_output(["git", "-C", str(repository), "archive", "--format=tar", base])
    archive_sha = hashlib.sha256(archive).hexdigest()
    if archive_sha != receipt["avibe_fixture_archive_sha256"]:
        raise RuntimeError("Frozen Avibe archive differs from the receipt.")
    root = Path(tempfile.mkdtemp(prefix="avibe-fixture-", dir=state))
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(root, filter="data")
    identity = {
        "commit": base, "tree": tree, "archive_sha256": archive_sha,
        "source_sha256": source_digest(root),
    }
    if "avibe_fixture_source_sha256" in receipt:
        verify_fixture(root, receipt["avibe_fixture_source_sha256"])
    return root, identity


def fixture_identity(root: Path, receipt: dict) -> dict:
    """Verify a copied export without trusting its working-copy Git metadata."""
    verify_fixture(root, receipt["avibe_fixture_source_sha256"])
    return {
        "commit": receipt["avibe_fixture_base"],
        "tree": receipt["avibe_fixture_tree"],
        "archive_sha256": receipt["avibe_fixture_archive_sha256"],
        "source_sha256": receipt["avibe_fixture_source_sha256"],
    }
