from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from core.memory.confined_filesystem import (
    ConfinedFilesystemError,
    PrivateSqliteDatabase,
    remove_anchored_entry,
    remove_confined_path,
)


def test_remove_confined_path_refuses_to_follow_a_swapped_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "managed"
    target.mkdir(mode=0o700)
    (target / "nested").mkdir(mode=0o700)
    (target / "nested" / "owned.txt").write_text("owned", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("outside must survive", encoding="utf-8")
    moved = home / "held-managed"

    from core.memory import confined_filesystem

    real_scandir = confined_filesystem.os.scandir
    swapped = False

    def swap_directory_for_symlink(path):
        nonlocal swapped
        if isinstance(path, int) and not swapped:
            swapped = True
            target.rename(moved)
            target.symlink_to(outside, target_is_directory=True)
        return real_scandir(path)

    monkeypatch.setattr(confined_filesystem.os, "scandir", swap_directory_for_symlink)
    with pytest.raises(ConfinedFilesystemError):
        remove_confined_path(home, target)

    assert swapped
    assert target.is_symlink()
    assert victim.read_text(encoding="utf-8") == "outside must survive"


def test_private_sqlite_database_prepares_and_hardens_owned_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / "state" / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)

    database.prepare()
    connection = database.connect()
    try:
        connection.execute("CREATE TABLE item (value TEXT NOT NULL)")
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o644)

    database.harden(sync_parent=True)

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert home.stat().st_mode & 0o777 == 0o700


def test_private_sqlite_database_rejects_an_unsafe_sidecar(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    outside = tmp_path / "outside"
    outside.write_bytes(b"")
    sidecar = path.with_name(f"{path.name}-wal")
    sidecar.symlink_to(outside)

    with pytest.raises(ConfinedFilesystemError):
        database.connect()


@pytest.mark.parametrize("unsafe_kind", ("hardlink", "fifo"))
def test_private_sqlite_database_rejects_unsafe_database_entries(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    path = home / "journal.sqlite"
    if unsafe_kind == "hardlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"")
        outside.chmod(0o600)
        os.link(outside, path)
    else:
        os.mkfifo(path, mode=0o600)

    with pytest.raises(ConfinedFilesystemError):
        PrivateSqliteDatabase(home, path).prepare()


def test_private_sqlite_database_rejects_unowned_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    path = home / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    real_uid = os.getuid()

    from core.memory import confined_filesystem

    monkeypatch.setattr(confined_filesystem.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(ConfinedFilesystemError):
        database.connect()


def test_remove_confined_path_unlinks_links_without_touching_their_victims(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("outside must survive", encoding="utf-8")

    symlink = home / "symlink"
    symlink.symlink_to(victim)
    hardlink = home / "hardlink"
    os.link(victim, hardlink)

    remove_confined_path(home, symlink)
    remove_confined_path(home, hardlink)

    assert not symlink.exists()
    assert not hardlink.exists()
    assert victim.read_text(encoding="utf-8") == "outside must survive"


def test_remove_confined_path_rejects_root_outside_and_special_entries(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("outside must survive", encoding="utf-8")
    fifo = home / "fifo"
    os.mkfifo(fifo, mode=0o600)

    with pytest.raises(ConfinedFilesystemError):
        remove_confined_path(home, home)
    with pytest.raises(ConfinedFilesystemError):
        remove_confined_path(home, outside)
    with pytest.raises(ConfinedFilesystemError):
        remove_confined_path(home, fifo)

    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
    assert outside.read_text(encoding="utf-8") == "outside must survive"


def test_remove_anchored_entry_requires_the_expected_directory_identity(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "target"
    target.mkdir(mode=0o700)
    identity = os.lstat(target)
    target.rename(home / "moved")
    target.mkdir(mode=0o700)
    descriptor = os.open(home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(ConfinedFilesystemError):
            remove_anchored_entry(
                descriptor,
                target.name,
                expected_identity=(identity.st_dev, identity.st_ino),
            )
    finally:
        os.close(descriptor)

    assert target.is_dir()
    assert (home / "moved").is_dir()
