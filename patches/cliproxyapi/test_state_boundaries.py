"""Actual pre-write consumers with fake homes and task-only sentinels."""

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import fixture
import isolation
import verify
import wire_matrix


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setattr(isolation.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(home)))
    for name in (".avibe", ".vibe_remote", ".codex", ".claude"):
        (home / name).mkdir()
        (home / name / "sentinel").write_bytes(b"unchanged test-only user state")
    return home


@pytest.mark.parametrize("name", [".avibe", ".vibe_remote", ".codex", ".claude"])
@pytest.mark.parametrize("consumer", ["environment", "sandbox", "export", "verify", "wire", "engine-seam"])
@pytest.mark.parametrize("alias", [False, True])
def test_actual_callers_refuse_before_any_protected_setup_write(tmp_path, fake_home, monkeypatch, name, consumer, alias):
    root = fake_home / name / "must-not-create"
    if alias:
        root = tmp_path / "task-alias"
        root.symlink_to(fake_home / name / "must-not-create")
    before = fixture.source_digest(fake_home)
    if consumer == "environment":
        action = lambda: isolation.isolated_environment(root)
    elif consumer == "sandbox":
        action = lambda: isolation.sandbox_prefix(root)
    elif consumer == "export":
        action = lambda: fixture.export_fixture(tmp_path / "not-a-repository", root, {})
    elif consumer == "engine-seam":
        action = lambda: wire_matrix.FrozenEngine(None, root, None, None)
    elif consumer == "verify":
        monkeypatch.setattr(sys, "argv", ["verify.py", "build", "--state", str(root),
                                         "--source", str(tmp_path / "not-a-source")])
        action = verify.main
    else:
        monkeypatch.setattr(sys, "argv", ["wire_matrix.py", "--state", str(root), "--binary", "/absent",
                                         "--fixture", "/absent", "--fixture-sha256", "0" * 64])
        action = wire_matrix.main
    with pytest.raises(ValueError, match="protected user state"):
        action()
    assert fixture.source_digest(fake_home) == before
    assert not (fake_home / name / "must-not-create").exists()


def test_protected_apply_source_refuses_before_git_or_patch(tmp_path, fake_home, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "verify.py", "apply", "--state", str(tmp_path / "task-state"),
        "--source", str(fake_home / ".avibe" / "source"),
    ])
    with pytest.raises(ValueError, match="protected user state"):
        verify.main()
    assert not (tmp_path / "task-state").exists()


def test_environment_aliases_are_all_validated_before_first_mkdir(tmp_path, fake_home):
    root = tmp_path / "task-state"
    root.mkdir()
    (root / "tmp").symlink_to(fake_home / ".codex")
    with pytest.raises(ValueError, match="protected user state"):
        isolation.isolated_environment(root)
    assert sorted(path.name for path in root.iterdir()) == ["tmp"]
    assert (fake_home / ".codex/sentinel").read_bytes() == b"unchanged test-only user state"


@pytest.mark.parametrize("which", ["home", "ancestor", "root", "tmp", "users"])
def test_broad_write_roots_refuse(tmp_path, fake_home, which):
    root = {"home": fake_home, "ancestor": tmp_path, "root": Path("/"),
            "tmp": Path("/tmp"), "users": Path("/Users")}[which]
    with pytest.raises(ValueError, match="dedicated, narrow"):
        isolation.isolated_environment(root)


def test_ordinary_dedicated_temporary_state_still_works(tmp_path, fake_home):
    root = tmp_path / "allowed-task"
    cache = tmp_path / "allowed-cache"
    environment = isolation.isolated_environment(root, cache=cache)
    assert Path(environment["HOME"]) == root / "home"
    assert Path(environment["GOCACHE"]) == cache / "cache"
    assert environment["GOTOOLCHAIN"] == "local"
    assert set(path.name for path in root.iterdir()) == {"home", "tmp", "config", "data"}


@pytest.mark.parametrize("root", ["/home/user/task", "/root/task", "/Users/user/task", "/tmp", "/var/tmp", "/"])
def test_privileged_domain_rejects_non_temporary_or_broad_roots(monkeypatch, root):
    # Pure virtual Linux path classification; never create/read those surfaces.
    monkeypatch.setattr(Path, "resolve", lambda path, **_kw: path)
    monkeypatch.setattr(isolation.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir="/home/user"))
    with pytest.raises(ValueError):
        isolation.validate_temporary_root(Path(root))


@pytest.mark.parametrize("root", ["/tmp/allocated-task", "/var/tmp/allocated/task"])
def test_privileged_domain_accepts_narrow_linux_temporary_roots(monkeypatch, root):
    monkeypatch.setattr(Path, "resolve", lambda path, **_kw: path)
    monkeypatch.setattr(isolation.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir="/home/user"))
    assert isolation.validate_temporary_root(Path(root)) == Path(root)


@pytest.mark.parametrize("target", ["/home/absent-task", "/root/absent-task", "/Users/absent-task"])
def test_symlinked_privileged_root_cannot_admit_a_hidden_surface(tmp_path, target):
    link = tmp_path / "looks-temporary"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        isolation.validate_temporary_root(link)
    assert link.is_symlink()


def test_privileged_validation_also_protects_invoking_users_aliased_state(tmp_path, monkeypatch):
    invoking_home, root_home = tmp_path / "invoking-home", tmp_path / "root-home"
    invoking_home.mkdir()
    root_home.mkdir()
    actual_data = tmp_path / "looks-like-temporary-data"
    actual_data.mkdir()
    (actual_data / "sentinel").write_bytes(b"invoking user's fake protected data")
    (invoking_home / ".avibe").symlink_to(actual_data)
    monkeypatch.setattr(isolation.os, "getuid", lambda: 0)
    monkeypatch.setattr(isolation.pwd, "getpwuid", lambda uid: SimpleNamespace(
        pw_dir=str(invoking_home if uid == 501 else root_home),
    ))
    with pytest.raises(ValueError, match="protected user state"):
        isolation.validate_state_root(actual_data / "evidence", owner_uid=501)
    assert list(actual_data.iterdir()) == [actual_data / "sentinel"]


@pytest.mark.parametrize("name", [".avibe", ".vibe_remote", ".codex", ".claude"])
def test_write_root_cannot_contain_protected_symlink_target(tmp_path, fake_home, name):
    root = tmp_path / "apparently-task-owned"
    target = root / "protected-target"
    target.mkdir(parents=True)
    (target / "sentinel").write_bytes(b"fake protected state must remain unchanged")
    protected = fake_home / name
    # Preserve the original fake home fixture while substituting only its alias.
    protected.rename(fake_home / (name + "-preserved"))
    protected.symlink_to(target)
    before = fixture.source_digest(root)
    with pytest.raises(ValueError, match="protected user state"):
        isolation.isolated_environment(root)
    assert fixture.source_digest(root) == before
