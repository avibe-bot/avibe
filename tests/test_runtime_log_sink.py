from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

from storage.lock import MigrationFileLock
from vibe import runtime
from vibe.log_sink import RUNTIME_LOG_TRUNCATION_MARKER, copy_bounded_log


def test_copy_bounded_log_preserves_inode_and_newest_output(tmp_path: Path) -> None:
    path = tmp_path / "service_stderr.log"
    path.write_bytes(b"old line\n" * 40)
    inode = path.stat().st_ino
    latest = b"latest diagnostic\n"

    copied = copy_bounded_log(
        io.BytesIO((b"new line\n" * 40) + latest),
        path,
        max_bytes=160,
        retain_bytes=80,
        chunk_bytes=32,
    )

    content = path.read_bytes()
    assert copied is True
    assert path.stat().st_ino == inode
    assert len(content) <= 160
    assert RUNTIME_LOG_TRUNCATION_MARKER in content
    assert content.endswith(latest)


def test_copy_bounded_log_skips_symlink_and_drains_input(tmp_path: Path) -> None:
    target = tmp_path / "outside.log"
    target.write_bytes(b"outside\n" * 100)
    link = tmp_path / "ui_stderr.log"
    link.symlink_to(target)
    source = io.BytesIO(b"new output")

    assert copy_bounded_log(source, link, max_bytes=80, retain_bytes=40) is False
    assert source.tell() == len(b"new output")
    assert target.read_bytes() == b"outside\n" * 100


def test_copy_bounded_log_drains_while_waiting_for_previous_sink(tmp_path: Path) -> None:
    path = tmp_path / "ui_stdout.log"
    lock = MigrationFileLock(path.with_name(f".{path.name}.sink.lock"))
    lock.acquire()
    source = io.BytesIO(b"noisy replacement startup\n" * 100)
    result: list[bool] = []
    thread = threading.Thread(
        target=lambda: result.append(
            copy_bounded_log(source, path, max_bytes=512, retain_bytes=256, chunk_bytes=64)
        )
    )
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while source.tell() < len(source.getvalue()) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert source.tell() == len(source.getvalue())
        time.sleep(0.35)
        assert thread.is_alive()
    finally:
        lock.release()
    thread.join(timeout=5)

    assert result == [True]
    assert path.read_bytes().endswith(b"noisy replacement startup\n")


def test_copy_bounded_log_flushes_quiet_startup_burst_after_lock_release(tmp_path: Path) -> None:
    class StartupBurst:
        def __init__(self) -> None:
            self.burst_read = threading.Event()
            self.finish = threading.Event()
            self.delivered = False

        def read1(self, _size: int) -> bytes:
            if not self.delivered:
                self.delivered = True
                self.burst_read.set()
                return b"startup complete\n"
            self.finish.wait(timeout=5)
            return b""

    path = tmp_path / "ui_stderr.log"
    lock = MigrationFileLock(path.with_name(f".{path.name}.sink.lock"))
    lock.acquire()
    source = StartupBurst()
    result: list[bool] = []
    thread = threading.Thread(
        target=lambda: result.append(
            copy_bounded_log(source, path, max_bytes=512, retain_bytes=256, chunk_bytes=64)
        )
    )
    thread.start()
    try:
        assert source.burst_read.wait(timeout=2)
        lock.release()
        deadline = time.monotonic() + 2
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert path.read_bytes() == b"startup complete\n"
        assert thread.is_alive()
    finally:
        lock.release()
        source.finish.set()
        thread.join(timeout=5)

    assert result == [True]


def test_spawned_process_output_is_continuously_bounded(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime.paths, "get_runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(runtime, "RUNTIME_LOG_MAX_BYTES", 512)
    monkeypatch.setattr(runtime, "RUNTIME_LOG_RETAIN_BYTES", 256)
    payload = "output-line-" * 500

    process = runtime.spawn_service_background_process(
        [sys.executable, "-c", f"print({payload!r})"],
        "service_stdout.log",
        "service_stderr.log",
    )
    assert process.wait(timeout=10) == 0

    stdout_path = tmp_path / "service_stdout.log"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if stdout_path.exists() and stdout_path.read_bytes().endswith(b"output-line-\n"):
            break
        time.sleep(0.05)

    content = stdout_path.read_bytes()
    assert len(content) <= 512
    assert content.endswith(b"output-line-\n")
