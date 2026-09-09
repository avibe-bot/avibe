"""Pure-source receipt regressions; no engine process or network access."""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from fixture import export_fixture, source_digest, verify_fixture
from isolation import isolated_environment


def frozen_repository(tmp_path: Path) -> tuple[Path, dict]:
    repository = tmp_path / "repository"
    repository.mkdir()
    env = isolated_environment(tmp_path / "state")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repository), *args], env=env, text=True,
        ).strip()

    git("init", "--quiet", "--template=")
    (repository / "fixture_model.py").write_text("MODEL = 'frozen-模型'\n")
    (repository / "facts").mkdir()
    (repository / "facts/config.json").write_text('{"model":"frozen-模型"}')
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "fixture")
    base = git("rev-parse", "HEAD")
    archive = subprocess.check_output(["git", "-C", str(repository), "archive", "--format=tar", base], env=env)
    return repository, {
        "avibe_fixture_base": base,
        "avibe_fixture_tree": git("rev-parse", f"{base}^{{tree}}"),
        "avibe_fixture_archive_sha256": hashlib.sha256(archive).hexdigest(),
    }


def test_frozen_export_never_imports_dirty_or_untracked_checkout(tmp_path: Path) -> None:
    repository, receipt = frozen_repository(tmp_path)
    (repository / "fixture_model.py").write_text("raise RuntimeError('dirty source executed')\n")
    (repository / "untracked.py").write_text("raise RuntimeError('untracked source executed')\n")
    exported, identity = export_fixture(repository, tmp_path, receipt)
    assert not (exported / "untracked.py").exists()
    assert identity["commit"] == receipt["avibe_fixture_base"]
    # A clean interpreter imports from the frozen export even with dirty cwd.
    result = subprocess.check_output(
        [sys.executable, "-I", "-B", "-c",
         "import sys; sys.path.insert(0,sys.argv[1]); import fixture_model; print(fixture_model.MODEL)",
         str(exported)], cwd=repository, env=isolated_environment(tmp_path / "child"), text=True,
    )
    assert result.strip() == "frozen-模型"
    verify_fixture(exported, identity["source_sha256"])


@pytest.mark.parametrize("mutation", ["content", "extra", "missing", "escape"])
def test_fixture_digest_rejects_changed_closure(tmp_path: Path, mutation: str) -> None:
    repository, receipt = frozen_repository(tmp_path)
    exported, identity = export_fixture(repository, tmp_path, receipt)
    if mutation == "content":
        (exported / "facts/config.json").write_text('{"model":"changed"}')
    elif mutation == "extra":
        (exported / "extra.py").write_text("VALUE = 1")
    elif mutation == "missing":
        (exported / "fixture_model.py").unlink()
    else:
        (exported / "outside.py").symlink_to(repository / "fixture_model.py")
    with pytest.raises((RuntimeError, ValueError)):
        verify_fixture(exported, identity["source_sha256"])


def test_wrong_archive_refuses_before_materializing_fixture(tmp_path: Path) -> None:
    repository, receipt = frozen_repository(tmp_path)
    receipt["avibe_fixture_archive_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="archive"):
        export_fixture(repository, tmp_path, receipt)
    assert not list(tmp_path.glob("avibe-fixture-*"))


def test_fixture_source_digest_is_path_independent(tmp_path: Path) -> None:
    repository, receipt = frozen_repository(tmp_path)
    first, identity = export_fixture(repository, tmp_path, receipt)
    second, second_identity = export_fixture(repository, tmp_path, receipt)
    assert first != second
    assert second_identity == identity
    assert source_digest(first) == source_digest(second)
