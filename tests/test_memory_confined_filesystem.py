from __future__ import annotations

import asyncio
import errno
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from core.memory.confined_filesystem import (
    ConfinedFilesystemError,
    PrivateSqliteDatabase,
    remove_anchored_entry,
    remove_confined_path,
    replace_confined,
)


def test_memory_persistence_fails_closed_without_no_follow_before_touching_path(
    tmp_path: Path,
) -> None:
    home = tmp_path / "must-not-exist"
    script = """
import os
import sys
import asyncio
from pathlib import Path
from types import SimpleNamespace

delattr(os, "O_NOFOLLOW")

# Importing ordinary application wiring must remain usable when only Memory
# persistence is unsupported on the host.
import core.controller  # noqa: F401
from core.memory.attachments import AttachmentPinError, AttachmentPinStore
from core.memory.confined_filesystem import ConfinedFilesystemError, PrivateSqliteDatabase
from config.v2_config import MemoryConfig
from core.memory.artifact import MemoryArtifactManager
from core.memory.runtime import create_memory_runtime
from core.memory.provider_root import ProviderRoot, ProviderRootError, ProviderRootMetadata
home = Path(sys.argv[1])
try:
    AttachmentPinStore(effective_home=home, source_root=home / "source")
except AttachmentPinError:
    pass
else:
    raise AssertionError("Memory attachments accepted a host without O_NOFOLLOW")
try:
    PrivateSqliteDatabase(home, home / "state" / "journal.sqlite").prepare()
except ConfinedFilesystemError as error:
    assert "no-follow" in str(error)
else:
    raise AssertionError("Memory persistence accepted a host without O_NOFOLLOW")
provider_root = ProviderRoot(home / "memory" / "everos-root", effective_home=home)
try:
    provider_root.ensure(
        SimpleNamespace(provider_root_id="root-id"),
        ProviderRootMetadata(
            provider_root_format="everos-1.0",
            compatible_provider_root_formats=frozenset({"everos-1.0"}),
            artifact_fingerprint="artifact",
        ),
    )
except ProviderRootError:
    pass
else:
    raise AssertionError("Memory provider root accepted a host without O_NOFOLLOW")
assert not home.exists()

for enabled in (False, True):
    runtime_home = home / ("enabled" if enabled else "disabled")
    artifact = MemoryArtifactManager(
        runtime_dir=runtime_home / "runtime" / "memory",
        provider_root=runtime_home / "memory" / "everos-root",
        offline=True,
    )
    runtime = create_memory_runtime(
        MemoryConfig(enabled=enabled),
        artifact_manager=artifact,
        effective_home=runtime_home,
    )
    assert runtime.available is False
    result = asyncio.run(runtime.reconcile(MemoryConfig(enabled=enabled)))
    if enabled:
        assert result == {"ok": False, "error": "memory_store_unavailable"}
    else:
        assert result == {"ok": True, "state": "disabled"}
    asyncio.run(runtime.close())
    assert not runtime_home.exists()
assert not home.exists()
"""

    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(home)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert not home.exists()


def test_supported_host_exposes_strict_no_follow_capability() -> None:
    from core.memory import confined_filesystem

    assert confined_filesystem.required_no_follow_flag() == os.O_NOFOLLOW


def test_spilled_directory_order_preserves_raw_filename_order_across_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.memory import confined_filesystem

    names = ["z", "a", "\udcff", "\udc80", "middle"]

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    class Scandir:
        def __enter__(self):
            return iter(Entry(name) for name in names)

        def __exit__(self, *_exc_info: object) -> None:
            return None

    monkeypatch.setattr(confined_filesystem.os, "scandir", lambda _fd: Scandir())
    with confined_filesystem.SpilledDirectoryOrder(insert_batch_size=2) as order:
        cursor = order.scan(-1)
        actual = list(order.names(cursor))

    assert [os.fsencode(name) for name in actual] == sorted(
        os.fsencode(name) for name in names
    )


def test_spilled_directory_order_translates_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.memory import confined_filesystem

    failure = sqlite3.OperationalError("temporary database unavailable")

    def fail_connect(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", fail_connect)

    with pytest.raises(ConfinedFilesystemError) as raised:
        confined_filesystem.SpilledDirectoryOrder()

    assert raised.value.__cause__ is failure


def test_spilled_directory_order_closes_and_translates_schema_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.memory import confined_filesystem

    real_connection = sqlite3.connect("")
    failure = sqlite3.OperationalError("temporary schema unavailable")

    class Connection:
        closed = False

        def execute(self, sql: str, parameters=()):
            if "CREATE TABLE directory_name" in sql:
                raise failure
            return real_connection.execute(sql, parameters)

        def close(self) -> None:
            self.closed = True
            real_connection.close()

    connection = Connection()
    monkeypatch.setattr(
        confined_filesystem.sqlite3,
        "connect",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(ConfinedFilesystemError) as raised:
        confined_filesystem.SpilledDirectoryOrder()

    assert raised.value.__cause__ is failure
    assert connection.closed is True
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        real_connection.execute("SELECT 1")


@pytest.mark.parametrize("failure_stage", ["insert", "read"])
def test_spilled_directory_order_closes_after_operation_database_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    from core.memory import confined_filesystem

    class Entry:
        name = "entry"

    class Scandir:
        def __enter__(self):
            return iter((Entry(),))

        def __exit__(self, *_exc_info: object) -> None:
            return None

    monkeypatch.setattr(confined_filesystem.os, "scandir", lambda _fd: Scandir())
    order = confined_filesystem.SpilledDirectoryOrder(insert_batch_size=1)
    real_connection = order._connection
    failure = sqlite3.OperationalError(f"temporary {failure_stage} failed")

    class Connection:
        closed = False

        def execute(self, sql: str, parameters=()):
            if failure_stage == "read" and "SELECT name" in sql:
                raise failure
            return real_connection.execute(sql, parameters)

        def executemany(self, sql: str, parameters):
            if failure_stage == "insert":
                raise failure
            return real_connection.executemany(sql, parameters)

        def close(self) -> None:
            self.closed = True
            real_connection.close()

    connection = Connection()
    if failure_stage == "read":
        cursor = order.scan(-1)
        order._connection = connection
        operation = lambda: order.next_name(cursor)
    else:
        order._connection = connection
        operation = lambda: order.scan(-1)

    with pytest.raises(ConfinedFilesystemError) as raised:
        operation()

    assert raised.value.__cause__ is failure
    assert connection.closed is True
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        real_connection.execute("SELECT 1")


def test_spilled_directory_order_closes_temporary_database_on_base_exception() -> None:
    from core.memory import confined_filesystem

    class InjectedCancellation(BaseException):
        pass

    order = confined_filesystem.SpilledDirectoryOrder(insert_batch_size=2)
    connection = order._connection

    with pytest.raises(InjectedCancellation):
        with order:
            raise InjectedCancellation()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("failed"), asyncio.CancelledError(), KeyboardInterrupt()],
)
def test_spilled_directory_order_closes_on_scan_failure_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    from core.memory import confined_filesystem

    class FailingScandir:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info: object) -> None:
            return None

        def __iter__(self):
            return self

        def __next__(self):
            raise failure

    monkeypatch.setattr(
        confined_filesystem.os,
        "scandir",
        lambda _fd: FailingScandir(),
    )
    order = confined_filesystem.SpilledDirectoryOrder(insert_batch_size=2)
    connection = order._connection

    with pytest.raises(type(failure)):
        with order:
            order.scan(-1)

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


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


def test_remove_confined_path_rejects_root_swap_before_directory_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "managed"
    target.mkdir(mode=0o700)
    held = home / "held-managed"

    from core.memory import confined_filesystem

    real_connect = confined_filesystem.sqlite3.connect
    swapped = False

    def swap_before_walk(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            target.rename(held)
            target.mkdir(mode=0o700)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", swap_before_walk)

    with pytest.raises(ConfinedFilesystemError, match="changed during removal"):
        remove_confined_path(home, target)

    assert swapped
    assert target.is_dir()
    assert held.is_dir()


def test_remove_confined_regular_file_does_not_initialize_directory_ordering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "current.json"
    target.write_text("pointer", encoding="utf-8")
    target.chmod(0o600)
    connect_calls = 0

    from core.memory import confined_filesystem

    def fail_connect(*_args, **_kwargs):
        nonlocal connect_calls
        connect_calls += 1
        raise sqlite3.OperationalError("temporary ordering unavailable")

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", fail_connect)

    remove_confined_path(home, target)

    assert connect_calls == 0
    assert not target.exists()


def test_confined_atomic_replace_refuses_symlink_source_and_replaces_symlink_target(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside survives", encoding="utf-8")
    stage = home / "stage"
    target = home / "target"

    stage.symlink_to(outside)
    with pytest.raises(ConfinedFilesystemError):
        replace_confined(home, stage, target)
    assert stage.is_symlink()
    assert not target.exists()

    stage.unlink()
    stage.write_text("published", encoding="utf-8")
    stage.chmod(0o600)
    target.symlink_to(outside)
    replace_confined(home, stage, target)

    assert target.read_text(encoding="utf-8") == "published"
    assert not target.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside survives"


def test_confined_atomic_replace_stays_on_pinned_parent_during_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    managed = home / "managed"
    managed.mkdir(mode=0o700)
    stage = managed / "stage"
    stage.write_text("published", encoding="utf-8")
    stage.chmod(0o600)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    sentinel = outside / "target"
    sentinel.write_text("outside survives", encoding="utf-8")
    held = home / "held-managed"

    from core.memory import confined_filesystem

    real_replace = confined_filesystem.os.replace
    swapped = False

    def swap_before_replace(source, destination, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            managed.rename(held)
            managed.symlink_to(outside, target_is_directory=True)
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(confined_filesystem.os, "replace", swap_before_replace)
    replace_confined(home, stage, managed / "target")

    assert swapped
    assert (held / "target").read_text(encoding="utf-8") == "published"
    assert sentinel.read_text(encoding="utf-8") == "outside survives"


@pytest.mark.parametrize("replacement_type", ["regular", "symlink"])
def test_confined_atomic_replace_cleans_a_source_replaced_during_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_type: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    stage = home / "stage"
    stage.write_text("original", encoding="utf-8")
    stage.chmod(0o600)
    target = home / "target"
    held = home / "held"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside survives", encoding="utf-8")

    from core.memory import confined_filesystem

    real_replace = confined_filesystem.os.replace

    def swap_source_before_replace(source, destination, *args, **kwargs):
        stage.rename(held)
        if replacement_type == "regular":
            stage.write_text("raced", encoding="utf-8")
            stage.chmod(0o600)
        else:
            stage.symlink_to(outside)
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(confined_filesystem.os, "replace", swap_source_before_replace)

    with pytest.raises(ConfinedFilesystemError):
        replace_confined(home, stage, target)

    assert not target.exists()
    assert not target.is_symlink()
    assert held.read_text(encoding="utf-8") == "original"
    assert outside.read_text(encoding="utf-8") == "outside survives"


def test_confined_atomic_replace_removes_a_source_made_public_during_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    stage = home / "stage"
    stage.mkdir(mode=0o700)
    value = stage / "value.txt"
    value.write_text("private", encoding="utf-8")
    value.chmod(0o600)
    target = home / "target"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside survives", encoding="utf-8")

    from core.memory import confined_filesystem

    real_replace = confined_filesystem.os.replace

    def expose_source_before_replace(source, destination, *args, **kwargs):
        stage.chmod(0o755)
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(confined_filesystem.os, "replace", expose_source_before_replace)

    with pytest.raises(ConfinedFilesystemError):
        replace_confined(home, stage, target)

    assert not target.exists()
    assert outside.read_text(encoding="utf-8") == "outside survives"


def test_confined_atomic_replace_removes_a_source_hardlinked_during_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    stage = home / "stage"
    stage.write_text("private", encoding="utf-8")
    stage.chmod(0o600)
    target = home / "target"
    outside_link = tmp_path / "outside-link.txt"

    from core.memory import confined_filesystem

    real_replace = confined_filesystem.os.replace

    def link_source_before_replace(source, destination, *args, **kwargs):
        os.link(stage, outside_link)
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(confined_filesystem.os, "replace", link_source_before_replace)

    with pytest.raises(ConfinedFilesystemError):
        replace_confined(home, stage, target)

    assert not target.exists()
    assert outside_link.read_text(encoding="utf-8") == "private"
    assert outside_link.stat().st_nlink == 1


def test_confined_replace_and_cleanup_accept_owner_only_directory_modes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    stage = home / "stage"
    stage.mkdir(mode=0o700)
    (stage / "value.txt").write_text("preserved", encoding="utf-8")
    stage.chmod(0o500)
    target = home / "target"

    replace_confined(home, stage, target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o500
    remove_confined_path(home, target)
    assert not target.exists()


@pytest.mark.parametrize("mode", (0o500, 0o755, 0o777))
def test_remove_confined_path_hardens_owned_directories(
    tmp_path: Path,
    mode: int,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "target"
    target.mkdir(mode=0o700)
    nested = target / "nested"
    nested.mkdir(mode=0o700)
    nested.chmod(mode)

    remove_confined_path(home, target)

    assert not target.exists()


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="requires an O_PATH inode anchor")
def test_remove_confined_path_hardens_an_inaccessible_owned_directory(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "target"
    target.mkdir(mode=0o700)
    nested = target / "nested"
    nested.mkdir(mode=0o700)
    nested.chmod(0o000)

    remove_confined_path(home, target)

    assert not target.exists()


def test_remove_confined_path_never_chmods_a_swapped_entry_without_an_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "target"
    target.mkdir(mode=0o700)
    locked = target / "locked"
    locked.mkdir(mode=0o700)
    locked.chmod(0o000)
    held = target / "held-locked"

    from core.memory import confined_filesystem

    real_open = confined_filesystem.os.open
    real_chmod = confined_filesystem.os.chmod
    path_chmod_called = False

    def refuse_inaccessible_directory(path, flags, *args, **kwargs):
        if path == locked.name and kwargs.get("dir_fd") is not None:
            raise PermissionError(errno.EACCES, "directory is inaccessible")
        return real_open(path, flags, *args, **kwargs)

    def swap_before_path_chmod(path, mode, *args, **kwargs):
        nonlocal path_chmod_called
        if path == locked.name and kwargs.get("dir_fd") is not None:
            path_chmod_called = True
            locked.rename(held)
            locked.mkdir(mode=0o755)
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.delattr(confined_filesystem.os, "O_PATH", raising=False)
    monkeypatch.setattr(confined_filesystem.os, "open", refuse_inaccessible_directory)
    monkeypatch.setattr(confined_filesystem.os, "chmod", swap_before_path_chmod)
    try:
        with pytest.raises(ConfinedFilesystemError, match="cannot be opened safely"):
            remove_confined_path(home, target)
    finally:
        if held.exists():
            real_chmod(held, 0o700)
        real_chmod(locked, 0o700)

    assert not path_chmod_called
    assert stat.S_IMODE(locked.stat().st_mode) == 0o700


@pytest.mark.parametrize("mode", (0o500, 0o755))
def test_remove_confined_path_does_not_require_procfs_for_openable_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "target"
    target.mkdir(mode=0o700)
    nested = target / "nested"
    nested.mkdir(mode=0o700)
    nested.chmod(mode)

    from core.memory import confined_filesystem

    fake_o_path = 1 << 30
    real_open = confined_filesystem.os.open
    real_chmod = confined_filesystem.os.chmod

    def emulate_o_path(path, flags, *args, **kwargs):
        if flags & fake_o_path:
            flags = (flags & ~fake_o_path) | os.O_RDONLY
        return real_open(path, flags, *args, **kwargs)

    def reject_procfs_chmod(path, chmod_mode, *args, **kwargs):
        if os.fspath(path).startswith("/proc/self/fd/"):
            raise FileNotFoundError(errno.ENOENT, "procfs is unavailable")
        return real_chmod(path, chmod_mode, *args, **kwargs)

    monkeypatch.setattr(confined_filesystem.os, "O_PATH", fake_o_path, raising=False)
    monkeypatch.setattr(confined_filesystem.os, "open", emulate_o_path)
    monkeypatch.setattr(confined_filesystem.os, "chmod", reject_procfs_chmod)

    remove_confined_path(home, target)

    assert not target.exists()


def test_remove_confined_path_rejects_a_mount_boundary_before_scanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "target"
    target.mkdir(mode=0o700)
    mounted = target / "mounted"
    mounted.mkdir(mode=0o700)
    victim = mounted / "victim.txt"
    victim.write_text("must survive", encoding="utf-8")

    from core.memory import confined_filesystem

    real_open = confined_filesystem.os.open
    real_scandir = confined_filesystem.os.scandir
    scanned_mount = False

    def reject_mount(parent_fd, name, flags):
        if name == mounted.name:
            raise OSError(errno.EXDEV, "mount boundary")
        return real_open(name, flags, dir_fd=parent_fd)

    def record_scandir(path):
        nonlocal scanned_mount
        if isinstance(path, int):
            opened = os.fstat(path)
            mounted_info = mounted.stat()
            if (opened.st_dev, opened.st_ino) == (
                mounted_info.st_dev,
                mounted_info.st_ino,
            ):
                scanned_mount = True
        return real_scandir(path)

    monkeypatch.setattr(
        confined_filesystem,
        "_open_directory_without_mount_crossing",
        reject_mount,
    )
    monkeypatch.setattr(confined_filesystem.os, "scandir", record_scandir)

    with pytest.raises(ConfinedFilesystemError, match="filesystem boundary"):
        remove_confined_path(home, target)

    assert not scanned_mount
    assert victim.read_text(encoding="utf-8") == "must survive"


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="requires an O_PATH inode anchor")
def test_remove_confined_path_rejects_directory_swap_during_permission_hardening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "target"
    target.mkdir(mode=0o700)
    locked = target / "locked"
    locked.mkdir(mode=0o700)
    (locked / "original.txt").write_text("original", encoding="utf-8")
    locked.chmod(0o000)
    held = target / "held-locked"

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod
    swapped = False

    def swap_before_chmod(path, mode, *args, **kwargs):
        nonlocal swapped
        anchored_path = os.fspath(path).startswith("/proc/self/fd/")
        if (
            not swapped
            and (path == locked.name or anchored_path)
            and (kwargs.get("dir_fd") is not None or anchored_path)
        ):
            swapped = True
            locked.rename(held)
            locked.mkdir(mode=0o700)
            (locked / "replacement.txt").write_text("replacement", encoding="utf-8")
            locked.chmod(0o000)
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(confined_filesystem.os, "chmod", swap_before_chmod)
    try:
        with pytest.raises(ConfinedFilesystemError, match="changed during removal"):
            remove_confined_path(home, target)
    finally:
        real_chmod(locked, 0o700)
        real_chmod(held, 0o700)

    assert swapped
    assert (locked / "replacement.txt").read_text(encoding="utf-8") == "replacement"
    assert (held / "original.txt").read_text(encoding="utf-8") == "original"


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="requires an O_PATH inode anchor")
def test_remove_confined_path_does_not_follow_symlink_swapped_in_during_hardening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "target"
    target.mkdir(mode=0o700)
    locked = target / "locked"
    locked.mkdir(mode=0o700)
    locked.chmod(0o000)
    held = target / "held-locked"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    victim = outside / "victim.txt"
    victim.write_text("outside survives", encoding="utf-8")

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod
    swapped = False

    def swap_before_chmod(path, mode, *args, **kwargs):
        nonlocal swapped
        anchored_path = os.fspath(path).startswith("/proc/self/fd/")
        if (
            not swapped
            and (path == locked.name or anchored_path)
            and (kwargs.get("dir_fd") is not None or anchored_path)
        ):
            swapped = True
            locked.rename(held)
            locked.symlink_to(outside, target_is_directory=True)
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(confined_filesystem.os, "chmod", swap_before_chmod)
    try:
        with pytest.raises(ConfinedFilesystemError):
            remove_confined_path(home, target)
    finally:
        real_chmod(held, 0o700)

    assert swapped
    assert locked.is_symlink()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert victim.read_text(encoding="utf-8") == "outside survives"


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="requires an O_PATH inode anchor")
def test_remove_confined_path_rechecks_device_after_permission_hardening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "target"
    target.mkdir(mode=0o700)
    target.chmod(0o000)

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod
    real_stat = confined_filesystem.os.stat
    hardened = False

    def mark_hardened(path, mode, *args, **kwargs):
        nonlocal hardened
        result = real_chmod(path, mode, *args, **kwargs)
        anchored_path = os.fspath(path).startswith("/proc/self/fd/")
        if (path == target.name and kwargs.get("dir_fd") is not None) or anchored_path:
            hardened = True
        return result

    def cross_device_after_hardening(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if path == target.name and hardened:
            values = list(info)
            values[2] = info.st_dev + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(confined_filesystem.os, "chmod", mark_hardened)
    monkeypatch.setattr(confined_filesystem.os, "stat", cross_device_after_hardening)
    try:
        with pytest.raises(ConfinedFilesystemError, match="filesystem boundary"):
            remove_confined_path(home, target)
    finally:
        target.chmod(0o700)

    assert hardened
    assert target.is_dir()


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="requires an O_PATH inode anchor")
def test_remove_confined_path_fails_closed_without_anchored_hardening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "target"
    target.mkdir(mode=0o700)
    target.chmod(0o000)

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod

    def unsupported_chmod(path, mode, *args, **kwargs):
        anchored_path = os.fspath(path).startswith("/proc/self/fd/")
        if kwargs.get("dir_fd") is not None or anchored_path:
            raise NotImplementedError("anchored chmod unavailable")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(confined_filesystem.os, "chmod", unsupported_chmod)
    try:
        with pytest.raises(ConfinedFilesystemError, match="cannot be hardened safely"):
            remove_confined_path(home, target)
    finally:
        real_chmod(target, 0o700)

    assert target.is_dir()


def test_remove_anchored_entry_rejects_foreign_owned_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "target"
    target.mkdir(mode=0o700)
    target.chmod(0o777)
    descriptor = os.open(home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    real_uid = os.getuid()

    from core.memory import confined_filesystem

    monkeypatch.setattr(confined_filesystem.os, "getuid", lambda: real_uid + 1)
    try:
        with pytest.raises(ConfinedFilesystemError):
            remove_anchored_entry(descriptor, target.name)
    finally:
        os.close(descriptor)

    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o777


def test_remove_anchored_entry_rejects_cross_device_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "target"
    target.mkdir(mode=0o700)
    target.chmod(0o777)
    descriptor = os.open(home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))

    from core.memory import confined_filesystem

    real_stat = confined_filesystem.os.stat

    def cross_device_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if path == target.name and kwargs.get("dir_fd") == descriptor:
            values = list(info)
            values[2] = info.st_dev + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(confined_filesystem.os, "stat", cross_device_stat)
    try:
        with pytest.raises(ConfinedFilesystemError, match="filesystem boundary"):
            remove_anchored_entry(descriptor, target.name)
    finally:
        os.close(descriptor)

    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o777


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


def test_private_sqlite_transaction_commits_before_hardening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    path = home / "state" / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    events: list[str] = []

    real_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):
            if sql == "BEGIN IMMEDIATE":
                events.append("begin")
            return super().execute(sql, parameters)

        def commit(self) -> None:
            events.append("commit")
            super().commit()

        def close(self) -> None:
            events.append("close")
            super().close()

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=TrackingConnection)

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod

    def chmod(target, mode: int) -> None:
        if Path(target) == path and mode == 0o600:
            events.append("harden")
        real_chmod(target, mode)

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", connect)
    monkeypatch.setattr(confined_filesystem.os, "chmod", chmod)

    with database.transaction() as connection:
        connection.execute("CREATE TABLE item (value TEXT NOT NULL)")
        connection.execute("INSERT INTO item VALUES ('committed')")

    with real_connect(path) as connection:
        assert connection.execute("SELECT value FROM item").fetchone() == (
            "committed",
        )
    assert events == ["begin", "commit", "close", "harden"]


def test_private_sqlite_transaction_body_failure_remains_primary_during_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    path = home / "state" / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    events: list[str] = []
    body_error = RuntimeError("body failed")

    real_connect = sqlite3.connect

    class FailingCleanupConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):
            if sql == "BEGIN IMMEDIATE":
                events.append("begin")
            return super().execute(sql, parameters)

        def rollback(self) -> None:
            events.append("rollback")
            super().rollback()
            raise OSError("rollback cleanup failed")

        def close(self) -> None:
            events.append("close")
            super().close()
            raise OSError("close cleanup failed")

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=FailingCleanupConnection)

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod

    def chmod(target, mode: int) -> None:
        if Path(target) == path and mode == 0o600:
            events.append("harden")
        real_chmod(target, mode)

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", connect)
    monkeypatch.setattr(confined_filesystem.os, "chmod", chmod)

    with pytest.raises(RuntimeError) as raised:
        with database.transaction() as connection:
            connection.execute("CREATE TABLE item (value TEXT NOT NULL)")
            raise body_error

    with real_connect(path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'item'"
        ).fetchone() is None
    assert raised.value is body_error
    assert events == ["begin", "rollback", "close", "harden"]


def test_private_sqlite_transaction_commit_failure_remains_primary_during_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    path = home / "state" / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    events: list[str] = []
    commit_error = sqlite3.OperationalError("commit failed")

    real_connect = sqlite3.connect

    class FailingCommitConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):
            if sql == "BEGIN IMMEDIATE":
                events.append("begin")
            return super().execute(sql, parameters)

        def commit(self) -> None:
            events.append("commit")
            raise commit_error

        def rollback(self) -> None:
            events.append("rollback")
            super().rollback()
            raise OSError("rollback cleanup failed")

        def close(self) -> None:
            events.append("close")
            super().close()
            raise OSError("close cleanup failed")

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=FailingCommitConnection)

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod

    def chmod(target, mode: int) -> None:
        if Path(target) == path and mode == 0o600:
            events.append("harden")
        real_chmod(target, mode)

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", connect)
    monkeypatch.setattr(confined_filesystem.os, "chmod", chmod)

    with pytest.raises(sqlite3.OperationalError) as raised:
        with database.transaction() as connection:
            connection.execute("CREATE TABLE item (value TEXT NOT NULL)")

    with real_connect(path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'item'"
        ).fetchone() is None
    assert raised.value is commit_error
    assert events == ["begin", "commit", "rollback", "close", "harden"]


def test_private_sqlite_transaction_rolls_back_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InjectedCancellation(BaseException):
        pass

    home = tmp_path / "home"
    path = home / "state" / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE item (value TEXT NOT NULL)")
    events: list[str] = []
    cancellation = InjectedCancellation()

    real_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):
            if sql == "BEGIN IMMEDIATE":
                events.append("begin")
            return super().execute(sql, parameters)

        def rollback(self) -> None:
            events.append("rollback")
            super().rollback()

        def close(self) -> None:
            events.append("close")
            super().close()

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=TrackingConnection)

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod

    def chmod(target, mode: int) -> None:
        if Path(target) == path and mode == 0o600:
            events.append("harden")
        real_chmod(target, mode)

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", connect)
    monkeypatch.setattr(confined_filesystem.os, "chmod", chmod)

    with pytest.raises(InjectedCancellation) as raised:
        with database.transaction() as connection:
            connection.execute("INSERT INTO item VALUES ('rolled back')")
            raise cancellation

    with real_connect(path) as connection:
        assert connection.execute("SELECT value FROM item").fetchall() == []
    assert raised.value is cancellation
    assert events == ["begin", "rollback", "close", "harden"]


def test_private_sqlite_transaction_translates_hardening_failure_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class JournalHardeningError(RuntimeError):
        pass

    home = tmp_path / "home"
    path = home / "state" / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    events: list[str] = []
    translated = JournalHardeningError("journal files could not be hardened safely")

    real_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):
            if sql == "BEGIN IMMEDIATE":
                events.append("begin")
            return super().execute(sql, parameters)

        def commit(self) -> None:
            events.append("commit")
            super().commit()

        def close(self) -> None:
            events.append("close")
            super().close()

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=TrackingConnection)

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod
    harden_error = ConfinedFilesystemError("unsafe SQLite sidecar")

    def chmod(target, mode: int) -> None:
        if Path(target) == path and mode == 0o600:
            events.append("harden")
            raise harden_error
        real_chmod(target, mode)

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", connect)
    monkeypatch.setattr(confined_filesystem.os, "chmod", chmod)

    with pytest.raises(JournalHardeningError) as raised:
        with database.transaction(
            translate_harden_error=lambda _error: translated,
        ) as connection:
            connection.execute("CREATE TABLE item (value TEXT NOT NULL)")
            connection.execute("INSERT INTO item VALUES ('durable')")

    with real_connect(path) as connection:
        assert connection.execute("SELECT value FROM item").fetchone() == ("durable",)
    assert raised.value is translated
    assert raised.value.__cause__ is harden_error
    assert events == ["begin", "commit", "close", "harden"]


def test_private_sqlite_transaction_closes_setup_failure_without_translation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    path = home / "state" / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    setup_error = sqlite3.OperationalError("SQLite setup failed")
    events: list[str] = []
    real_connect = sqlite3.connect

    class FailingSetupConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):
            if sql == "PRAGMA synchronous=FULL":
                raise setup_error
            return super().execute(sql, parameters)

        def close(self) -> None:
            events.append("close")
            super().close()

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=FailingSetupConnection)

    from core.memory import confined_filesystem

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", connect)

    with pytest.raises(sqlite3.OperationalError) as raised:
        with database.transaction():
            pytest.fail("transaction body must not run after setup failure")

    assert raised.value is setup_error
    assert events == ["close"]


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
